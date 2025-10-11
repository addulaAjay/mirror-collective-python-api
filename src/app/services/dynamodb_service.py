"""
DynamoDB service for user profile management
"""

import logging
import uuid
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aioboto3
from botocore.exceptions import ClientError

from ..core.exceptions import InternalServerError
from ..models.conversation import Conversation, ConversationMessage
from ..models.user_profile import UserProfile

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
        self.conversations_table = os.getenv(
            "DYNAMODB_CONVERSATIONS_TABLE", "conversations"
        )
        self.messages_table = os.getenv(
            "DYNAMODB_MESSAGES_TABLE", "conversation_messages"
        )
        self.endpoint_url = os.getenv("DYNAMODB_ENDPOINT_URL")  # For local DynamoDB

        # MirrorGPT table configuration
        self.archetype_profiles_table = os.getenv(
            "DYNAMODB_ARCHETYPE_PROFILES_TABLE", "user_archetype_profiles"
        )
        self.mirror_moments_table = os.getenv(
            "DYNAMODB_MIRROR_MOMENTS_TABLE", "mirror_moments"
        )
        self.pattern_loops_table = os.getenv(
            "DYNAMODB_PATTERN_LOOPS_TABLE", "pattern_loops"
        )
        self.quiz_results_table = os.getenv(
            "DYNAMODB_QUIZ_RESULTS_TABLE", "archetype_quiz_results"
        )
        self.echo_vault_table = os.getenv("DYNAMODB_ECHO_VAULT_TABLE", "echo_vault")

        # Initialize aioboto3 session
        self.session = aioboto3.Session()

        # Log configuration
        target = "Local DynamoDB" if self.endpoint_url else "AWS DynamoDB"
        logger.info(
            f"DynamoDB service initialized - Target: {target}, Region: {self.region}, Users Table: {self.users_table}"
        )
        logger.info(f"MirrorGPT tables - Profiles: {self.archetype_profiles_table}")
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

    async def record_echo_vault_entry(
        self,
        *,
        user_id: str,
        media_type: str,
        s3_bucket: str,
        s3_key: str,
        object_url: str,
        content_type: str,
    ) -> Dict[str, Any]:
        """Insert an Echo Vault entry linking a user with stored media."""
        item = {
            "user_id": user_id,
            "vault_id": f"{int(datetime.now(timezone.utc).timestamp()*1000)}-{uuid.uuid4().hex[:8]}",
            "media_type": media_type,
            "s3_bucket": s3_bucket,
            "s3_key": s3_key,
            "object_url": object_url,
            "content_type": content_type,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.echo_vault_table)
                await table.put_item(Item=item)
                logger.info(
                    f"EchoVault entry created: user={user_id}, type={media_type}, key={s3_key}"
                )
                return item
        except ClientError as e:
            logger.error(f"DynamoDB error writing echo_vault: {e}")
            raise InternalServerError(f"Failed to record echo_vault entry: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error writing echo_vault: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")

    

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
                    ConditionExpression="attribute_not_exists(conversation_id)",
                )

                logger.info(
                    f"Created conversation {conversation.conversation_id} for user {conversation.user_id}"
                )
                return conversation

        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                logger.error(
                    f"Conversation {conversation.conversation_id} already exists"
                )
                raise InternalServerError("Conversation already exists")
            else:
                logger.error(f"DynamoDB error creating conversation: {e}")
                raise InternalServerError(f"Failed to create conversation: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error creating conversation: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")

    async def get_conversation(
        self, conversation_id: str, user_id: str
    ) -> Optional[Conversation]:
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
                    Key={"conversation_id": conversation_id, "user_id": user_id}
                )

                if "Item" in response:
                    conversation = Conversation.from_dynamodb_item(response["Item"])
                    # Security check: ensure conversation belongs to the requesting user
                    if conversation.user_id == user_id:
                        return conversation
                    else:
                        logger.warning(
                            f"User {user_id} attempted to access conversation {conversation_id} owned by {conversation.user_id}"
                        )
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
                    ":last_message_at": conversation.last_message_at,
                }

                await table.update_item(
                    Key={
                        "conversation_id": conversation.conversation_id,
                        "user_id": conversation.user_id,
                    },
                    UpdateExpression=update_expression,
                    ExpressionAttributeNames=expression_attribute_names,
                    ExpressionAttributeValues=expression_attribute_values,
                )

                return conversation

        except ClientError as e:
            logger.error(f"DynamoDB error updating conversation: {e}")
            raise InternalServerError(f"Failed to update conversation: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error updating conversation: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")

    async def get_user_conversations(
        self, user_id: str, limit: int = 50, include_archived: bool = False
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
                    Limit=limit,
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
                response = await table.get_item(
                    Key={"conversation_id": conversation_id, "user_id": user_id}
                )
                if "Item" not in response:
                    return False

                conversation_item = response["Item"]

                await table.update_item(
                    Key={"conversation_id": conversation_id, "user_id": user_id},
                    UpdateExpression="SET is_archived = :archived, updated_at = :updated_at",
                    ExpressionAttributeValues={
                        ":archived": True,
                        ":updated_at": datetime.now(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                    },
                )

                logger.info(
                    f"Archived conversation {conversation_id} for user {user_id}"
                )
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
                    ExpressionAttributeValues={":conversation_id": conversation_id},
                )

                # Delete messages in batches
                if response.get("Items"):
                    with messages_table.batch_writer() as batch:
                        for item in response["Items"]:
                            batch.delete_item(
                                Key={
                                    "conversation_id": item["conversation_id"],
                                    "timestamp": item["timestamp"],
                                }
                            )

                # Delete the conversation
                conversations_table = await dynamodb.Table(self.conversations_table)

                # First verify the conversation belongs to the user
                conversation_response = await conversations_table.get_item(
                    Key={"conversation_id": conversation_id, "user_id": user_id}
                )
                if "Item" not in conversation_response:
                    return False

                conversation_item = conversation_response["Item"]

                await conversations_table.delete_item(
                    Key={"conversation_id": conversation_id, "user_id": user_id}
                )

                logger.info(
                    f"Deleted conversation {conversation_id} and its messages for user {user_id}"
                )
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

                logger.debug(
                    f"Created message {message.message_id} in conversation {message.conversation_id}"
                )
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
        last_evaluated_key: Optional[Dict] = None,
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
                    "ScanIndexForward": True,  # Sort by timestamp ascending
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
        self, conversation_id: str, limit: int = 20
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
                    Limit=limit,
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

    # ========================================
    # MIRRORGPT ARCHETYPE PROFILE METHODS
    # ========================================

    async def get_user_archetype_profile(
        self, user_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get user's archetype profile

        Args:
            user_id: User ID

        Returns:
            Dict containing archetype profile data or None
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.archetype_profiles_table)

                response = await table.get_item(Key={"user_id": user_id})

                if "Item" in response:
                    return dict(response["Item"])
                return None

        except ClientError as e:
            logger.error(f"DynamoDB error getting archetype profile: {e}")
            raise InternalServerError(f"Failed to get archetype profile: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error getting archetype profile: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")

    async def save_user_archetype_profile(
        self, profile_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Save user's archetype profile

        Args:
            profile_data: Complete profile data dictionary

        Returns:
            The saved profile data
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.archetype_profiles_table)

                await table.put_item(Item=profile_data)

                logger.info(
                    f"Saved archetype profile for user {profile_data.get('user_id')}"
                )
                return profile_data

        except ClientError as e:
            logger.error(f"DynamoDB error saving archetype profile: {e}")
            raise InternalServerError(f"Failed to save archetype profile: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error saving archetype profile: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")

    # ========================================
    # MIRROR MOMENTS METHODS
    # ========================================

    async def save_mirror_moment(self, moment_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Save Mirror Moment

        Args:
            moment_data: Mirror moment data

        Returns:
            The saved moment data
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.mirror_moments_table)

                await table.put_item(Item=moment_data)

                logger.info(
                    f"Saved mirror moment {moment_data.get('moment_id')} for user {moment_data.get('user_id')}"
                )
                return moment_data

        except ClientError as e:
            logger.error(f"DynamoDB error saving mirror moment: {e}")
            raise InternalServerError(f"Failed to save mirror moment: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error saving mirror moment: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")

    async def get_user_mirror_moments(
        self, user_id: str, limit: int = 10, acknowledged_only: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Get user's Mirror Moments

        Args:
            user_id: User ID
            limit: Maximum number of moments
            acknowledged_only: Filter for acknowledged moments only

        Returns:
            List of mirror moments
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.mirror_moments_table)

                query_kwargs: Dict[str, Any] = {
                    "KeyConditionExpression": "user_id = :user_id",
                    "ExpressionAttributeValues": {":user_id": user_id},
                    "ScanIndexForward": False,  # Most recent first
                    "Limit": limit,
                }

                if acknowledged_only:
                    query_kwargs["FilterExpression"] = "acknowledged = :ack"
                    if "ExpressionAttributeValues" not in query_kwargs:
                        query_kwargs["ExpressionAttributeValues"] = {}
                    query_kwargs["ExpressionAttributeValues"][":ack"] = True

                response = await table.query(**query_kwargs)

                return [dict(item) for item in response.get("Items", [])]

        except ClientError as e:
            logger.error(f"DynamoDB error getting mirror moments: {e}")
            raise InternalServerError(f"Failed to get mirror moments: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error getting mirror moments: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")

    async def acknowledge_mirror_moment(self, user_id: str, moment_id: str) -> bool:
        """
        Acknowledge a Mirror Moment

        Args:
            user_id: User ID
            moment_id: Moment ID to acknowledge

        Returns:
            True if successful
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.mirror_moments_table)

                current_time = (
                    datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                )

                await table.update_item(
                    Key={"user_id": user_id, "moment_id": moment_id},
                    UpdateExpression="SET acknowledged = :ack, acknowledged_at = :timestamp",
                    ExpressionAttributeValues={
                        ":ack": True,
                        ":timestamp": current_time,
                    },
                )

                logger.info(
                    f"Acknowledged mirror moment {moment_id} for user {user_id}"
                )
                return True

        except ClientError as e:
            logger.error(f"DynamoDB error acknowledging mirror moment: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error acknowledging mirror moment: {e}")
            return False

    # ========================================
    # PATTERN LOOPS METHODS
    # ========================================

    async def save_pattern_loop(self, loop_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Save pattern loop data

        Args:
            loop_data: Pattern loop data

        Returns:
            The saved loop data
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.pattern_loops_table)

                await table.put_item(Item=loop_data)

                logger.debug(
                    f"Saved pattern loop {loop_data.get('loop_id')} for user {loop_data.get('user_id')}"
                )
                return loop_data

        except ClientError as e:
            logger.error(f"DynamoDB error saving pattern loop: {e}")
            raise InternalServerError(f"Failed to save pattern loop: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error saving pattern loop: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")

    async def get_user_pattern_loops(
        self, user_id: str, active_only: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Get user's pattern loops

        Args:
            user_id: User ID
            active_only: Filter for active loops only

        Returns:
            List of pattern loops
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.pattern_loops_table)

                query_kwargs: Dict[str, Any] = {
                    "KeyConditionExpression": "user_id = :user_id",
                    "ExpressionAttributeValues": {":user_id": user_id},
                    "ScanIndexForward": False,
                }

                if active_only:
                    query_kwargs["FilterExpression"] = (
                        "#trend IN (:rising, :stable) AND #transformed <> :true"
                    )
                    query_kwargs["ExpressionAttributeNames"] = {
                        "#trend": "trend",
                        "#transformed": "transformation_detected",
                    }
                    if "ExpressionAttributeValues" not in query_kwargs:
                        query_kwargs["ExpressionAttributeValues"] = {}
                    query_kwargs["ExpressionAttributeValues"].update(
                        {":rising": "rising", ":stable": "stable", ":true": True}
                    )

                response = await table.query(**query_kwargs)

                return [dict(item) for item in response.get("Items", [])]

        except ClientError as e:
            logger.error(f"DynamoDB error getting pattern loops: {e}")
            raise InternalServerError(f"Failed to get pattern loops: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error getting pattern loops: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")

    async def update_pattern_loop(
        self, user_id: str, loop_id: str, updates: Dict[str, Any]
    ) -> bool:
        """
        Update a pattern loop

        Args:
            user_id: User ID
            loop_id: Loop ID
            updates: Dictionary of fields to update

        Returns:
            True if successful
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.pattern_loops_table)

                # Build update expression dynamically
                update_expressions = []
                expression_values = {}
                expression_names = {}

                for field, value in updates.items():
                    if field in [
                        "trend",
                        "strength_score",
                        "transformation_detected",
                        "last_seen",
                        "occurrence_count",
                    ]:
                        attr_name = f"#{field}"
                        attr_value = f":{field}"

                        update_expressions.append(f"{attr_name} = {attr_value}")
                        expression_names[attr_name] = field
                        expression_values[attr_value] = value

                if not update_expressions:
                    return False

                update_expression = "SET " + ", ".join(update_expressions)

                await table.update_item(
                    Key={"user_id": user_id, "loop_id": loop_id},
                    UpdateExpression=update_expression,
                    ExpressionAttributeNames=expression_names,
                    ExpressionAttributeValues=expression_values,
                )

                logger.debug(f"Updated pattern loop {loop_id} for user {user_id}")
                return True

        except ClientError as e:
            logger.error(f"DynamoDB error updating pattern loop: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error updating pattern loop: {e}")
            return False

    # ========================================
    # HELPER METHODS FOR MIRRORGPT
    # ========================================

    async def get_item(
        self, table_name: str, key: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Generic get item method

        Args:
            table_name: DynamoDB table name
            key: Primary key for item

        Returns:
            Item data or None
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(table_name)

                response = await table.get_item(Key=key)

                if "Item" in response:
                    return dict(response["Item"])
                return None

        except ClientError as e:
            logger.error(f"DynamoDB error getting item from {table_name}: {e}")
            raise InternalServerError(f"Failed to get item: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error getting item from {table_name}: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")

    async def put_item(self, table_name: str, item: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generic put item method

        Args:
            table_name: DynamoDB table name
            item: Item data to store

        Returns:
            The stored item data
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(table_name)

                await table.put_item(Item=item)

                return item

        except ClientError as e:
            logger.error(f"DynamoDB error putting item to {table_name}: {e}")
            raise InternalServerError(f"Failed to put item: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error putting item to {table_name}: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")

    async def query_items(
        self,
        table_name: str,
        key_condition: str,
        expression_values: Dict[str, Any],
        limit: Optional[int] = None,
        scan_index_forward: bool = True,
        filter_expression: Optional[str] = None,
        expression_names: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Generic query method for DynamoDB tables

        Args:
            table_name: DynamoDB table name
            key_condition: KeyConditionExpression
            expression_values: ExpressionAttributeValues
            limit: Limit number of items
            scan_index_forward: Sort order
            filter_expression: FilterExpression
            expression_names: ExpressionAttributeNames

        Returns:
            List of items
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(table_name)

                query_kwargs = {
                    "KeyConditionExpression": key_condition,
                    "ExpressionAttributeValues": expression_values,
                    "ScanIndexForward": scan_index_forward,
                }

                if limit:
                    query_kwargs["Limit"] = limit

                if filter_expression:
                    query_kwargs["FilterExpression"] = filter_expression

                if expression_names:
                    query_kwargs["ExpressionAttributeNames"] = expression_names

                response = await table.query(**query_kwargs)

                return [dict(item) for item in response.get("Items", [])]

        except ClientError as e:
            logger.error(f"DynamoDB error querying {table_name}: {e}")
            raise InternalServerError(f"Failed to query items: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error querying {table_name}: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")

    async def update_item(
        self,
        table_name: str,
        key: Dict[str, Any],
        update_expression: str,
        expression_values: Dict[str, Any],
        expression_names: Optional[Dict[str, str]] = None,
    ) -> bool:
        """
        Generic update item method

        Args:
            table_name: DynamoDB table name
            key: Primary key for item
            update_expression: UpdateExpression
            expression_values: ExpressionAttributeValues
            expression_names: ExpressionAttributeNames

        Returns:
            True if successful
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(table_name)

                update_kwargs = {
                    "Key": key,
                    "UpdateExpression": update_expression,
                    "ExpressionAttributeValues": expression_values,
                }

                if expression_names:
                    update_kwargs["ExpressionAttributeNames"] = expression_names

                await table.update_item(**update_kwargs)

                return True

        except ClientError as e:
            logger.error(f"DynamoDB error updating item in {table_name}: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error updating item in {table_name}: {e}")
            return False

    async def save_quiz_results(self, quiz_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Save archetype quiz results

        Args:
            quiz_data: Quiz results data including user_id, answers, and archetype

        Returns:
            Dict with success status and quiz_id
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.quiz_results_table)

                # Add quiz_id as partition key if not present
                if "quiz_id" not in quiz_data:
                    quiz_data["quiz_id"] = (
                        f"quiz_{quiz_data['user_id']}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
                    )

                await table.put_item(Item=quiz_data)

                logger.info(f"Quiz results saved for user {quiz_data['user_id']}")

                return {
                    "success": True,
                    "quiz_id": quiz_data["quiz_id"],
                    "user_id": quiz_data["user_id"],
                }

        except ClientError as e:
            logger.error(f"DynamoDB error saving quiz results: {e}")
            return {"success": False, "error": str(e)}
        except Exception as e:
            logger.error(f"Unexpected error saving quiz results: {e}")
            return {"success": False, "error": str(e)}
