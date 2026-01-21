"""
Create MirrorGPT DynamoDB tables
Script to create all required DynamoDB tables for MirrorGPT functionality
Note: echo_signals table removed - MirrorGPT analysis now stored in conversation_messages
"""

import logging
import os
import sys

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

# Load environment variables from .env
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
            aws_access_key_id="dummy",
            aws_secret_access_key="dummy",
        )
    else:
        # AWS DynamoDB configuration
        logger.info(f"Using AWS DynamoDB in region: {AWS_REGION}")
        return boto3.client("dynamodb", region_name=AWS_REGION)


def create_mirrorgpt_tables():
    """Create all MirrorGPT DynamoDB tables"""

    dynamodb = get_dynamodb_client()

    # Define table configurations
    # Note: echo_signals table removed - MirrorGPT analysis now stored in conversation_messages
    tables = [
        {
            "TableName": os.getenv(
                "DYNAMODB_ARCHETYPE_PROFILES_TABLE", "user_archetype_profiles"
            ),
            "KeySchema": [{"AttributeName": "user_id", "KeyType": "HASH"}],
            "AttributeDefinitions": [
                {"AttributeName": "user_id", "AttributeType": "S"}
            ],
            "BillingMode": "PAY_PER_REQUEST",
            "Tags": [
                {"Key": "Service", "Value": "MirrorGPT"},
                {"Key": "Component", "Value": "ArchetypeProfiles"},
                {
                    "Key": "Environment",
                    "Value": os.getenv("ENVIRONMENT", "development"),
                },
            ],
        },
        {
            "TableName": os.getenv("DYNAMODB_MIRROR_MOMENTS_TABLE", "mirror_moments"),
            "KeySchema": [
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "moment_id", "KeyType": "RANGE"},
            ],
            "AttributeDefinitions": [
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "moment_id", "AttributeType": "S"},
                {"AttributeName": "triggered_at", "AttributeType": "S"},
                {"AttributeName": "moment_type", "AttributeType": "S"},
            ],
            "GlobalSecondaryIndexes": [
                {
                    "IndexName": "type-index",
                    "KeySchema": [
                        {"AttributeName": "user_id", "KeyType": "HASH"},
                        {"AttributeName": "moment_type", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "time-index",
                    "KeySchema": [
                        {"AttributeName": "user_id", "KeyType": "HASH"},
                        {"AttributeName": "triggered_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            "BillingMode": "PAY_PER_REQUEST",
            "Tags": [
                {"Key": "Service", "Value": "MirrorGPT"},
                {"Key": "Component", "Value": "MirrorMoments"},
                {
                    "Key": "Environment",
                    "Value": os.getenv("ENVIRONMENT", "development"),
                },
            ],
        },
        {
            "TableName": os.getenv("DYNAMODB_PATTERN_LOOPS_TABLE", "pattern_loops"),
            "KeySchema": [
                {"AttributeName": "user_id", "KeyType": "HASH"},
                {"AttributeName": "loop_id", "KeyType": "RANGE"},
            ],
            "AttributeDefinitions": [
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "loop_id", "AttributeType": "S"},
                {"AttributeName": "last_seen", "AttributeType": "S"},
                {"AttributeName": "trend", "AttributeType": "S"},
            ],
            "GlobalSecondaryIndexes": [
                {
                    "IndexName": "trend-index",
                    "KeySchema": [
                        {"AttributeName": "user_id", "KeyType": "HASH"},
                        {"AttributeName": "trend", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "activity-index",
                    "KeySchema": [
                        {"AttributeName": "user_id", "KeyType": "HASH"},
                        {"AttributeName": "last_seen", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            "BillingMode": "PAY_PER_REQUEST",
            "Tags": [
                {"Key": "Service", "Value": "MirrorGPT"},
                {"Key": "Component", "Value": "PatternLoops"},
                {
                    "Key": "Environment",
                    "Value": os.getenv("ENVIRONMENT", "development"),
                },
            ],
        },
        {
            "TableName": os.getenv("DYNAMODB_QUIZ_QUESTIONS_TABLE", "quiz_questions"),
            "KeySchema": [{"AttributeName": "id", "KeyType": "HASH"}],
            "AttributeDefinitions": [{"AttributeName": "id", "AttributeType": "N"}],
            "BillingMode": "PAY_PER_REQUEST",
            "Tags": [
                {"Key": "Service", "Value": "MirrorGPT"},
                {"Key": "Component", "Value": "QuizQuestions"},
                {
                    "Key": "Environment",
                    "Value": os.getenv("ENVIRONMENT", "development"),
                },
            ],
        },
        {
            "TableName": os.getenv(
                "DYNAMODB_QUIZ_RESULTS_TABLE", "archetype_quiz_results"
            ),
            "KeySchema": [{"AttributeName": "quiz_id", "KeyType": "HASH"}],
            "AttributeDefinitions": [
                {"AttributeName": "quiz_id", "AttributeType": "S"},
                {"AttributeName": "user_id", "AttributeType": "S"},
            ],
            "GlobalSecondaryIndexes": [
                {
                    "IndexName": "user-index",
                    "KeySchema": [{"AttributeName": "user_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            "BillingMode": "PAY_PER_REQUEST",
            "Tags": [
                {"Key": "Service", "Value": "MirrorGPT"},
                {"Key": "Component", "Value": "QuizResults"},
                {
                    "Key": "Environment",
                    "Value": os.getenv("ENVIRONMENT", "development"),
                },
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

            # Create the table
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
    logger.info("MIRRORGPT TABLE CREATION SUMMARY")
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
    logger.info(
        "Use 'aws dynamodb describe-table --table-name <table_name>' to check status."
    )
    logger.info(
        "\n📝 echo_signals table removed - MirrorGPT analysis now stored in conversation_messages"
    )

    return True


def verify_tables():
    """Verify that all MirrorGPT tables exist and are active"""

    dynamodb = get_dynamodb_client()

    # Note: echo_signals removed from table list
    table_names = [
        os.getenv("DYNAMODB_ARCHETYPE_PROFILES_TABLE", "user_archetype_profiles"),
        os.getenv("DYNAMODB_MIRROR_MOMENTS_TABLE", "mirror_moments"),
        os.getenv("DYNAMODB_PATTERN_LOOPS_TABLE", "pattern_loops"),
        os.getenv("DYNAMODB_QUIZ_QUESTIONS_TABLE", "quiz_questions"),
    ]

    logger.info("\n" + "=" * 50)
    logger.info("VERIFYING MIRRORGPT TABLES")
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
        logger.info("\n🎉 All MirrorGPT tables are active and ready!")
    else:
        logger.warning(
            "\n⚠️  Some tables are not active yet. Please wait and try again."
        )

    logger.info(
        "\n📝 Note: echo_signals table is no longer used (MirrorGPT analysis stored in conversation_messages)"
    )

    return all_active


def delete_mirrorgpt_tables():
    """Delete all MirrorGPT tables (use with caution!)"""

    print("\n⚠️  WARNING: This will delete all MirrorGPT tables and their data!")
    confirmation = input("Type 'DELETE' to confirm: ")

    if confirmation != "DELETE":
        print("Operation cancelled.")
        return False

    dynamodb = get_dynamodb_client()

    # Note: echo_signals removed from deletion list
    table_names = [
        os.getenv("DYNAMODB_ARCHETYPE_PROFILES_TABLE", "user_archetype_profiles"),
        os.getenv("DYNAMODB_MIRROR_MOMENTS_TABLE", "mirror_moments"),
        os.getenv("DYNAMODB_PATTERN_LOOPS_TABLE", "pattern_loops"),
        os.getenv("DYNAMODB_QUIZ_QUESTIONS_TABLE", "quiz_questions"),
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
    logger.info("📝 Note: echo_signals table was already removed from configuration")
    return True


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Manage MirrorGPT DynamoDB tables")
    parser.add_argument(
        "action", choices=["create", "verify", "delete"], help="Action to perform"
    )
    parser.add_argument(
        "--force", action="store_true", help="Skip confirmation prompts"
    )

    args = parser.parse_args()

    logger.info("MirrorGPT DynamoDB Table Management")
    logger.info(f"Environment: {os.getenv('ENVIRONMENT', 'development')}")
    logger.info(f"AWS Region: {AWS_REGION}")

    if os.getenv("DYNAMODB_ENDPOINT_URL"):
        logger.info(f"DynamoDB Endpoint: {os.getenv('DYNAMODB_ENDPOINT_URL')} (Local)")
    else:
        logger.info("DynamoDB Endpoint: AWS (Cloud)")

    logger.info(
        "📝 Note: echo_signals table removed - MirrorGPT analysis now in conversation_messages"
    )

    if args.action == "create":
        success = create_mirrorgpt_tables()
        sys.exit(0 if success else 1)

    elif args.action == "verify":
        success = verify_tables()
        sys.exit(0 if success else 1)

    elif args.action == "delete":
        if args.force:
            success = delete_mirrorgpt_tables()
        else:
            print("Use --force flag to confirm deletion")
            success = False
        sys.exit(0 if success else 1)

    else:
        parser.print_help()
        sys.exit(1)
