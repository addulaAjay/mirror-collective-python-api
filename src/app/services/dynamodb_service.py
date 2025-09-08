"""
DynamoDB service for user profile management
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aioboto3
from botocore.exceptions import ClientError

from ..core.exceptions import InternalServerError
from ..models.user_profile import UserProfile
from ..models.conversation import Conversation, ConversationMessage

logger = logging.getLogger(__name__)


class DynamoDBService:
    """
    Service for managing user profiles and activity in DynamoDB
    """

    def __init__(self):
        """Initialize DynamoDB service"""
        self.region = os.getenv("AWS_REGION", "us-east-1")
        self.users_table = os.getenv("DYNAMODB_USERS_TABLE", "users")
        self.activity_table = os.getenv("DYNAMODB_ACTIVITY_TABLE", "user_activity")
        self.conversations_table = os.getenv("DYNAMODB_CONVERSATIONS_TABLE", "conversations")
        self.messages_table = os.getenv("DYNAMODB_MESSAGES_TABLE", "conversation_messages")
        self.endpoint_url = os.getenv("DYNAMODB_ENDPOINT_URL")  # For local DynamoDB

        # Initialize aioboto3 session
        self.session = aioboto3.Session()

        # Log configuration
        target = "Local DynamoDB" if self.endpoint_url else "AWS DynamoDB"
        logger.info(
            f"DynamoDB service initialized - Target: {target}, Region: {self.region}, Users Table: {self.users_table}"
        )
        if self.endpoint_url:
            logger.info(f"Using local DynamoDB endpoint: {self.endpoint_url}")

    def _get_dynamodb_kwargs(self):
        """Get DynamoDB connection parameters (local or AWS)"""
        kwargs = {"region_name": self.region}

        if self.endpoint_url:
            # Local DynamoDB configuration
            kwargs.update(
                {
                    "endpoint_url": self.endpoint_url,
                    "aws_access_key_id": "dummy",
                    "aws_secret_access_key": "dummy",
                }
            )

        return kwargs

    async def get_user_profile(self, user_id: str) -> Optional[UserProfile]:
        """
        Get user profile by user ID

        Args:
            user_id: Cognito sub (UUID)

        Returns:
            UserProfile if found, None otherwise
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.users_table)

                response = await table.get_item(Key={"user_id": user_id})

                if "Item" in response:
                    return UserProfile.from_dynamodb_item(response["Item"])
                return None

        except ClientError as e:
            logger.error(f"DynamoDB error getting user profile {user_id}: {e}")
            raise InternalServerError(f"Failed to get user profile: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error getting user profile {user_id}: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")

    async def create_user_profile(self, user_profile: UserProfile) -> UserProfile:
        """
        Create a new user profile

        Args:
            user_profile: UserProfile to create

        Returns:
            Created UserProfile
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.users_table)

                item = user_profile.to_dynamodb_item()

                # Use condition to prevent overwriting existing users
                await table.put_item(
                    Item=item, ConditionExpression="attribute_not_exists(user_id)"
                )

                logger.info(f"Created user profile for {user_profile.user_id}")
                return user_profile

        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                logger.warning(f"User profile already exists: {user_profile.user_id}")
                # Return existing profile
                existing_profile = await self.get_user_profile(user_profile.user_id)
                if existing_profile is None:
                    raise InternalServerError(
                        f"User profile should exist but could not be retrieved: {user_profile.user_id}"
                    )
                return existing_profile
            else:
                logger.error(f"DynamoDB error creating user profile: {e}")
                raise InternalServerError(f"Failed to create user profile: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error creating user profile: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")

    async def update_user_profile(self, user_profile: UserProfile) -> UserProfile:
        """
        Update existing user profile

        Args:
            user_profile: UserProfile with updated data

        Returns:
            Updated UserProfile
        """
        try:
            # Update the timestamp
            user_profile.updated_at = (
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            )

            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.users_table)

                item = user_profile.to_dynamodb_item()

                await table.put_item(Item=item)

                logger.info(f"Updated user profile for {user_profile.user_id}")
                return user_profile

        except ClientError as e:
            logger.error(f"DynamoDB error updating user profile: {e}")
            raise InternalServerError(f"Failed to update user profile: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error updating user profile: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")

    async def delete_user_profile(self, user_id: str) -> bool:
        """
        Delete user profile (for account deletion)

        Args:
            user_id: Cognito sub (UUID)

        Returns:
            True if deleted successfully
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.users_table)

                await table.delete_item(Key={"user_id": user_id})

                logger.info(f"Deleted user profile for {user_id}")
                return True

        except ClientError as e:
            logger.error(f"DynamoDB error deleting user profile: {e}")
            raise InternalServerError(f"Failed to delete user profile: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error deleting user profile: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")

    async def sync_user_with_cognito(
        self, user_id: str, cognito_user_data: Dict[str, Any]
    ) -> UserProfile:
        """
        Sync user profile with latest Cognito data

        Args:
            user_id: Cognito sub (UUID)
            cognito_user_data: Data from Cognito GetUser/AdminGetUser

        Returns:
            Updated UserProfile
        """
        try:
            # Get existing profile or create new one
            existing_profile = await self.get_user_profile(user_id)

            if existing_profile:
                # Update existing profile with Cognito data
                existing_profile.update_from_cognito(cognito_user_data)
                return await self.update_user_profile(existing_profile)
            else:
                # Create new profile from Cognito data
                new_profile = UserProfile.from_cognito_user(cognito_user_data, user_id)
                return await self.create_user_profile(new_profile)

        except Exception as e:
            logger.error(f"Error syncing user with Cognito: {e}")
            raise InternalServerError(f"Failed to sync user with Cognito: {str(e)}")

    async def record_user_activity(self, user_id: str, activity_type: str) -> None:
        """
        Record user activity for analytics

        Args:
            user_id: Cognito sub (UUID)
            activity_type: Type of activity ('chat', 'login', etc.)
        """
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            current_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.activity_table)

                # Use atomic updates to increment counters
                key = {"user_id": user_id, "activity_date": today}

                if activity_type == "chat":
                    await table.update_item(
                        Key=key,
                        UpdateExpression="ADD chat_messages :inc SET last_chat_at = :time",
                        ExpressionAttributeValues={":inc": 1, ":time": current_time},
                    )
                elif activity_type == "login":
                    await table.update_item(
                        Key=key,
                        UpdateExpression="ADD login_count :inc SET last_login_at = :time",
                        ExpressionAttributeValues={":inc": 1, ":time": current_time},
                    )

                # Also update the user profile's conversation count if it's a chat
                if activity_type == "chat":
                    users_table = await dynamodb.Table(self.users_table)
                    await users_table.update_item(
                        Key={"user_id": user_id},
                        UpdateExpression="ADD conversation_count :inc SET updated_at = :time",
                        ExpressionAttributeValues={":inc": 1, ":time": current_time},
                    )

        except ClientError as e:
            logger.error(f"DynamoDB error recording activity: {e}")
            # Don't raise error for activity tracking failures
        except Exception as e:
            logger.error(f"Unexpected error recording activity: {e}")

    async def get_user_by_email(self, email: str) -> Optional[UserProfile]:
        """
        Get user profile by email (using GSI)

        Args:
            email: User's email address

        Returns:
            UserProfile if found, None otherwise
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.users_table)

                # Query GSI on email
                response = await table.query(
                    IndexName="email-index",
                    KeyConditionExpression="email = :email",
                    ExpressionAttributeValues={":email": email},
                )

                if response["Items"]:
                    return UserProfile.from_dynamodb_item(response["Items"][0])
                return None

        except ClientError as e:
            logger.error(f"DynamoDB error getting user by email: {e}")
            raise InternalServerError(f"Failed to get user by email: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error getting user by email: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")

    async def update_last_login(self, user_id: str) -> None:
        """
        Update user's last login timestamp

        Args:
            user_id: Cognito sub (UUID)
        """
        try:
            current_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.users_table)

                await table.update_item(
                    Key={"user_id": user_id},
                    UpdateExpression="SET last_login_at = :time, updated_at = :time",
                    ExpressionAttributeValues={":time": current_time},
                )

                # Also record login activity
                await self.record_user_activity(user_id, "login")

        except Exception as e:
            logger.error(f"Error updating last login: {e}")
            # Don't raise error for login timestamp failures

    # ========================================
    # CONVERSATION MANAGEMENT METHODS
    # ========================================

    async def create_conversation(self, conversation: Conversation) -> Conversation:
        """
        Create a new conversation in DynamoDB

        Args:
            conversation: Conversation object to create

        Returns:
            Conversation: The created conversation

        Raises:
            InternalServerError: If conversation creation fails
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.conversations_table)

                # Convert to DynamoDB item
                item = conversation.to_dynamodb_item()

                await table.put_item(
                    Item=item,
                    ConditionExpression="attribute_not_exists(conversation_id)"
                )

                logger.info(f"Created conversation {conversation.conversation_id} for user {conversation.user_id}")
                return conversation

        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                logger.error(f"Conversation {conversation.conversation_id} already exists")
                raise InternalServerError("Conversation already exists")
            else:
                logger.error(f"DynamoDB error creating conversation: {e}")
                raise InternalServerError(f"Failed to create conversation: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error creating conversation: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")

    async def get_conversation(self, conversation_id: str, user_id: str) -> Optional[Conversation]:
        """
        Get a conversation by ID and user ID

        Args:
            conversation_id: The conversation ID
            user_id: The user ID (for security)

        Returns:
            Optional[Conversation]: The conversation if found and belongs to user
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.conversations_table)

                response = await table.get_item(
                    Key={"conversation_id": conversation_id}
                )

                if "Item" in response:
                    conversation = Conversation.from_dynamodb_item(response["Item"])
                    # Security check: ensure conversation belongs to the requesting user
                    if conversation.user_id == user_id:
                        return conversation
                    else:
                        logger.warning(f"User {user_id} attempted to access conversation {conversation_id} owned by {conversation.user_id}")
                        return None
                return None

        except ClientError as e:
            logger.error(f"DynamoDB error getting conversation: {e}")
            raise InternalServerError(f"Failed to get conversation: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error getting conversation: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")

    async def update_conversation(self, conversation: Conversation) -> Conversation:
        """
        Update an existing conversation

        Args:
            conversation: Updated conversation object

        Returns:
            Conversation: The updated conversation
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.conversations_table)

                # Update only specific fields to avoid overwriting
                update_expression = "SET #title = :title, updated_at = :updated_at, message_count = :message_count, total_tokens = :total_tokens, last_message_at = :last_message_at"
                expression_attribute_names = {"#title": "title"}
                expression_attribute_values = {
                    ":title": conversation.title,
                    ":updated_at": conversation.updated_at,
                    ":message_count": conversation.message_count,
                    ":total_tokens": conversation.total_tokens,
                    ":last_message_at": conversation.last_message_at
                }

                await table.update_item(
                    Key={"conversation_id": conversation.conversation_id},
                    UpdateExpression=update_expression,
                    ExpressionAttributeNames=expression_attribute_names,
                    ExpressionAttributeValues=expression_attribute_values
                )

                return conversation

        except ClientError as e:
            logger.error(f"DynamoDB error updating conversation: {e}")
            raise InternalServerError(f"Failed to update conversation: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error updating conversation: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")

    async def get_user_conversations(
        self, 
        user_id: str, 
        limit: int = 50,
        include_archived: bool = False
    ) -> List[Conversation]:
        """
        Get all conversations for a user, sorted by last activity

        Args:
            user_id: The user ID
            limit: Maximum number of conversations to return
            include_archived: Whether to include archived conversations

        Returns:
            List[Conversation]: User's conversations
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.conversations_table)

                # Query using GSI (user_id + last_message_at)
                response = await table.query(
                    IndexName="user-conversations-index",
                    KeyConditionExpression="user_id = :user_id",
                    ExpressionAttributeValues={":user_id": user_id},
                    ScanIndexForward=False,  # Sort by last_message_at descending
                    Limit=limit
                )

                conversations = []
                for item in response.get("Items", []):
                    conversation = Conversation.from_dynamodb_item(item)
                    
                    # Filter archived conversations if not requested
                    if not include_archived and conversation.is_archived:
                        continue
                        
                    conversations.append(conversation)

                return conversations

        except ClientError as e:
            logger.error(f"DynamoDB error getting user conversations: {e}")
            raise InternalServerError(f"Failed to get user conversations: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error getting user conversations: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")

    async def archive_conversation(self, conversation_id: str, user_id: str) -> bool:
        """
        Archive a conversation (soft delete)

        Args:
            conversation_id: The conversation ID
            user_id: The user ID (for security validation)

        Returns:
            bool: True if archived successfully
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.conversations_table)

                # First verify the conversation belongs to the user
                response = await table.get_item(Key={"conversation_id": conversation_id})
                if "Item" not in response:
                    return False
                
                conversation_item = response["Item"]
                if conversation_item.get("user_id") != user_id:
                    logger.warning(f"User {user_id} attempted to archive conversation {conversation_id} owned by {conversation_item.get('user_id')}")
                    return False

                await table.update_item(
                    Key={"conversation_id": conversation_id},
                    UpdateExpression="SET is_archived = :archived, updated_at = :updated_at",
                    ExpressionAttributeValues={
                        ":archived": True,
                        ":updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    }
                )

                logger.info(f"Archived conversation {conversation_id} for user {user_id}")
                return True

        except ClientError as e:
            logger.error(f"DynamoDB error archiving conversation: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error archiving conversation: {e}")
            return False

    async def delete_conversation(self, conversation_id: str, user_id: str) -> bool:
        """
        Delete a conversation and all its messages (hard delete)

        Args:
            conversation_id: The conversation ID
            user_id: The user ID

        Returns:
            bool: True if deleted successfully
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                
                # Delete all messages first
                messages_table = await dynamodb.Table(self.messages_table)
                
                # Query all messages for this conversation
                response = await messages_table.query(
                    KeyConditionExpression="conversation_id = :conversation_id",
                    ExpressionAttributeValues={":conversation_id": conversation_id}
                )

                # Delete messages in batches
                if response.get("Items"):
                    with messages_table.batch_writer() as batch:
                        for item in response["Items"]:
                            batch.delete_item(
                                Key={
                                    "conversation_id": item["conversation_id"],
                                    "timestamp": item["timestamp"]
                                }
                            )

                # Delete the conversation
                conversations_table = await dynamodb.Table(self.conversations_table)
                
                # First verify the conversation belongs to the user
                conversation_response = await conversations_table.get_item(Key={"conversation_id": conversation_id})
                if "Item" not in conversation_response:
                    return False
                
                conversation_item = conversation_response["Item"]
                if conversation_item.get("user_id") != user_id:
                    logger.warning(f"User {user_id} attempted to delete conversation {conversation_id} owned by {conversation_item.get('user_id')}")
                    return False
                
                await conversations_table.delete_item(
                    Key={"conversation_id": conversation_id}
                )

                logger.info(f"Deleted conversation {conversation_id} and its messages for user {user_id}")
                return True

        except ClientError as e:
            logger.error(f"DynamoDB error deleting conversation: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error deleting conversation: {e}")
            return False

    # ========================================
    # MESSAGE MANAGEMENT METHODS
    # ========================================

    async def create_message(self, message: ConversationMessage) -> ConversationMessage:
        """
        Create a new message in a conversation

        Args:
            message: ConversationMessage object to create

        Returns:
            ConversationMessage: The created message
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.messages_table)

                # Convert to DynamoDB item
                item = message.to_dynamodb_item()

                await table.put_item(Item=item)

                logger.debug(f"Created message {message.message_id} in conversation {message.conversation_id}")
                return message

        except ClientError as e:
            logger.error(f"DynamoDB error creating message: {e}")
            raise InternalServerError(f"Failed to create message: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error creating message: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")

    async def get_conversation_messages(
        self, 
        conversation_id: str, 
        limit: Optional[int] = None,
        last_evaluated_key: Optional[Dict] = None
    ) -> tuple[List[ConversationMessage], Optional[Dict]]:
        """
        Get messages for a conversation with pagination

        Args:
            conversation_id: The conversation ID
            limit: Maximum number of messages to return
            last_evaluated_key: Pagination key from previous request

        Returns:
            tuple: (messages, next_pagination_key)
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.messages_table)

                query_kwargs = {
                    "KeyConditionExpression": "conversation_id = :conversation_id",
                    "ExpressionAttributeValues": {":conversation_id": conversation_id},
                    "ScanIndexForward": True  # Sort by timestamp ascending
                }

                if limit:
                    query_kwargs["Limit"] = limit

                if last_evaluated_key:
                    query_kwargs["ExclusiveStartKey"] = last_evaluated_key

                response = await table.query(**query_kwargs)

                messages = []
                for item in response.get("Items", []):
                    messages.append(ConversationMessage.from_dynamodb_item(item))

                # Return pagination key if there are more items
                next_key = response.get("LastEvaluatedKey")

                return messages, next_key

        except ClientError as e:
            logger.error(f"DynamoDB error getting messages: {e}")
            raise InternalServerError(f"Failed to get messages: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error getting messages: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")

    async def get_recent_messages(
        self, 
        conversation_id: str, 
        limit: int = 20
    ) -> List[ConversationMessage]:
        """
        Get the most recent messages for a conversation

        Args:
            conversation_id: The conversation ID
            limit: Number of recent messages to return

        Returns:
            List[ConversationMessage]: Recent messages in chronological order
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.messages_table)

                response = await table.query(
                    KeyConditionExpression="conversation_id = :conversation_id",
                    ExpressionAttributeValues={":conversation_id": conversation_id},
                    ScanIndexForward=False,  # Get newest first
                    Limit=limit
                )

                messages = []
                for item in response.get("Items", []):
                    messages.append(ConversationMessage.from_dynamodb_item(item))

                # Reverse to get chronological order (oldest first)
                messages.reverse()
                return messages

        except ClientError as e:
            logger.error(f"DynamoDB error getting recent messages: {e}")
            raise InternalServerError(f"Failed to get recent messages: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error getting recent messages: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")
