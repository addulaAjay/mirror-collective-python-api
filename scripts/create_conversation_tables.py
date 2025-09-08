"""
Production-ready DynamoDB table creation script for conversation management
Creates optimized tables with proper indexing for scalability
"""

import asyncio
import logging
import os
from typing import Dict, Any

import aioboto3
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class ConversationTableManager:
    """
    Manages DynamoDB table creation and configuration for conversation system
    """
    
    def __init__(self):
        self.region = os.getenv("AWS_REGION", "us-east-1")
        self.endpoint_url = os.getenv("DYNAMODB_ENDPOINT_URL")  # For local DynamoDB
        self.session = aioboto3.Session()
        
        # Table names from environment or defaults
        self.conversations_table = os.getenv("DYNAMODB_CONVERSATIONS_TABLE", "mirror-conversations")
        self.messages_table = os.getenv("DYNAMODB_MESSAGES_TABLE", "mirror-conversation-messages")
        
        logger.info(f"Initialized ConversationTableManager - Region: {self.region}")
        if self.endpoint_url:
            logger.info(f"Using local DynamoDB endpoint: {self.endpoint_url}")

    def _get_dynamodb_kwargs(self) -> Dict[str, Any]:
        """Get DynamoDB connection parameters"""
        kwargs = {"region_name": self.region}
        
        if self.endpoint_url:
            # Local DynamoDB configuration
            kwargs.update({
                "endpoint_url": self.endpoint_url,
                "aws_access_key_id": "dummy",
                "aws_secret_access_key": "dummy"
            })
        
        return kwargs

    async def create_conversations_table(self) -> bool:
        """
        Create the conversations table with optimized schema
        
        Schema:
        - Primary Key: conversation_id (HASH) + user_id (RANGE)
        - GSI: user_id (HASH) + last_message_at (RANGE) for listing user conversations
        
        Returns:
            bool: True if created successfully
        """
        try:
            async with self.session.client("dynamodb", **self._get_dynamodb_kwargs()) as dynamodb:
                
                table_definition = {
                    "TableName": self.conversations_table,
                    "KeySchema": [
                        {"AttributeName": "conversation_id", "KeyType": "HASH"},
                        {"AttributeName": "user_id", "KeyType": "RANGE"}
                    ],
                    "AttributeDefinitions": [
                        {"AttributeName": "conversation_id", "AttributeType": "S"},
                        {"AttributeName": "user_id", "AttributeType": "S"},
                        {"AttributeName": "last_message_at", "AttributeType": "S"}
                    ],
                    "GlobalSecondaryIndexes": [
                        {
                            "IndexName": "user-conversations-index",
                            "KeySchema": [
                                {"AttributeName": "user_id", "KeyType": "HASH"},
                                {"AttributeName": "last_message_at", "KeyType": "RANGE"}
                            ],
                            "Projection": {"ProjectionType": "ALL"}
                        }
                    ],
                    "BillingMode": "PAY_PER_REQUEST",
                    "StreamSpecification": {
                        "StreamEnabled": False
                    },
                    "SSESpecification": {
                        "Enabled": True
                    },
                    "Tags": [
                        {"Key": "Application", "Value": "mirror-collective"},
                        {"Key": "Environment", "Value": os.getenv("ENVIRONMENT", "development")},
                        {"Key": "Purpose", "Value": "conversation-metadata"}
                    ]
                }
                
                # For local DynamoDB, remove features not supported
                if self.endpoint_url:
                    # Remove SSE and tags for local DynamoDB
                    del table_definition["SSESpecification"]
                    del table_definition["Tags"]
                    # Set billing mode for local
                    table_definition["BillingMode"] = "PAY_PER_REQUEST"
                
                await dynamodb.create_table(**table_definition)
                logger.info(f"Created conversations table: {self.conversations_table}")
                
                # Wait for table to be active
                await self._wait_for_table_active(dynamodb, self.conversations_table)
                return True
                
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceInUseException":
                logger.warning(f"Conversations table {self.conversations_table} already exists")
                return True
            else:
                logger.error(f"Error creating conversations table: {e}")
                return False
        except Exception as e:
            logger.error(f"Unexpected error creating conversations table: {e}")
            return False

    async def create_messages_table(self) -> bool:
        """
        Create the conversation messages table with optimized schema
        
        Schema:
        - Primary Key: conversation_id (HASH) + timestamp (RANGE)
        - Optimized for chronological message retrieval
        
        Returns:
            bool: True if created successfully
        """
        try:
            async with self.session.client("dynamodb", **self._get_dynamodb_kwargs()) as dynamodb:
                
                table_definition = {
                    "TableName": self.messages_table,
                    "KeySchema": [
                        {"AttributeName": "conversation_id", "KeyType": "HASH"},
                        {"AttributeName": "timestamp", "KeyType": "RANGE"}
                    ],
                    "AttributeDefinitions": [
                        {"AttributeName": "conversation_id", "AttributeType": "S"},
                        {"AttributeName": "timestamp", "AttributeType": "S"}
                    ],
                    "BillingMode": "PAY_PER_REQUEST",
                    "StreamSpecification": {
                        "StreamEnabled": False
                    },
                    "SSESpecification": {
                        "Enabled": True
                    },
                    "Tags": [
                        {"Key": "Application", "Value": "mirror-collective"},
                        {"Key": "Environment", "Value": os.getenv("ENVIRONMENT", "development")},
                        {"Key": "Purpose", "Value": "conversation-messages"}
                    ]
                }
                
                # For local DynamoDB, remove features not supported
                if self.endpoint_url:
                    del table_definition["SSESpecification"]
                    del table_definition["Tags"]
                    table_definition["BillingMode"] = "PAY_PER_REQUEST"
                
                await dynamodb.create_table(**table_definition)
                logger.info(f"Created messages table: {self.messages_table}")
                
                # Wait for table to be active
                await self._wait_for_table_active(dynamodb, self.messages_table)
                return True
                
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceInUseException":
                logger.warning(f"Messages table {self.messages_table} already exists")
                return True
            else:
                logger.error(f"Error creating messages table: {e}")
                return False
        except Exception as e:
            logger.error(f"Unexpected error creating messages table: {e}")
            return False

    async def _wait_for_table_active(self, dynamodb_client, table_name: str, max_wait_time: int = 300):
        """
        Wait for a table to become active
        
        Args:
            dynamodb_client: DynamoDB client
            table_name: Name of the table to wait for
            max_wait_time: Maximum time to wait in seconds
        """
        import time
        
        start_time = time.time()
        
        while time.time() - start_time < max_wait_time:
            try:
                response = await dynamodb_client.describe_table(TableName=table_name)
                status = response["Table"]["TableStatus"]
                
                if status == "ACTIVE":
                    logger.info(f"Table {table_name} is now active")
                    return
                
                logger.info(f"Table {table_name} status: {status}, waiting...")
                await asyncio.sleep(5)
                
            except Exception as e:
                logger.warning(f"Error checking table status: {e}")
                await asyncio.sleep(5)
        
        logger.warning(f"Table {table_name} did not become active within {max_wait_time} seconds")

    async def create_all_tables(self) -> bool:
        """
        Create all conversation-related tables
        
        Returns:
            bool: True if all tables created successfully
        """
        logger.info("Starting creation of conversation management tables...")
        
        # Create conversations table
        conversations_success = await self.create_conversations_table()
        
        # Create messages table
        messages_success = await self.create_messages_table()
        
        success = conversations_success and messages_success
        
        if success:
            logger.info("✅ All conversation tables created successfully!")
            await self._log_table_info()
        else:
            logger.error("❌ Failed to create some conversation tables")
        
        return success

    async def _log_table_info(self):
        """Log information about created tables"""
        try:
            async with self.session.client("dynamodb", **self._get_dynamodb_kwargs()) as dynamodb:
                
                # Get conversations table info
                conv_response = await dynamodb.describe_table(TableName=self.conversations_table)
                conv_table = conv_response["Table"]
                
                # Get messages table info
                msg_response = await dynamodb.describe_table(TableName=self.messages_table)
                msg_table = msg_response["Table"]
                
                logger.info(f"""
📊 Table Creation Summary:
┌─────────────────────────────────────────┐
│ Conversations Table                     │
├─────────────────────────────────────────┤
│ Name: {conv_table['TableName']:<30} │
│ Status: {conv_table['TableStatus']:<28} │
│ Items: {conv_table.get('ItemCount', 0):<29} │
└─────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│ Messages Table                          │
├─────────────────────────────────────────┤
│ Name: {msg_table['TableName']:<30} │
│ Status: {msg_table['TableStatus']:<28} │
│ Items: {msg_table.get('ItemCount', 0):<29} │
└─────────────────────────────────────────┘

🚀 Conversation management system ready!
                """)
                
        except Exception as e:
            logger.warning(f"Could not retrieve table info: {e}")

    async def delete_all_tables(self) -> bool:
        """
        Delete all conversation tables (for cleanup/testing)
        
        Returns:
            bool: True if all tables deleted successfully
        """
        logger.warning("⚠️  DELETING ALL CONVERSATION TABLES - THIS CANNOT BE UNDONE!")
        
        try:
            async with self.session.client("dynamodb", **self._get_dynamodb_kwargs()) as dynamodb:
                
                # Delete conversations table
                try:
                    await dynamodb.delete_table(TableName=self.conversations_table)
                    logger.info(f"Deleted conversations table: {self.conversations_table}")
                except ClientError as e:
                    if e.response["Error"]["Code"] != "ResourceNotFoundException":
                        logger.error(f"Error deleting conversations table: {e}")
                
                # Delete messages table
                try:
                    await dynamodb.delete_table(TableName=self.messages_table)
                    logger.info(f"Deleted messages table: {self.messages_table}")
                except ClientError as e:
                    if e.response["Error"]["Code"] != "ResourceNotFoundException":
                        logger.error(f"Error deleting messages table: {e}")
                
                logger.info("✅ All conversation tables deleted")
                return True
                
        except Exception as e:
            logger.error(f"Error during table deletion: {e}")
            return False


async def main():
    """Main function to create conversation tables"""
    manager = ConversationTableManager()
    
    # Check command line arguments
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--delete":
        print("🚨 This will DELETE all conversation tables!")
        confirm = input("Type 'DELETE' to confirm: ")
        if confirm == "DELETE":
            success = await manager.delete_all_tables()
        else:
            print("Operation cancelled")
            return
    else:
        success = await manager.create_all_tables()
    
    if not success:
        exit(1)


if __name__ == "__main__":
    asyncio.run(main())
