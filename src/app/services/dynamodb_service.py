"""
DynamoDB service for user profile management
"""
import os
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
import aioboto3
from botocore.exceptions import ClientError

from ..models.user_profile import UserProfile, UserActivity, UserStatus
from ..core.exceptions import UserNotFoundError, InternalServerError

logger = logging.getLogger(__name__)


class DynamoDBService:
    """
    Service for managing user profiles and activity in DynamoDB
    """
    
    def __init__(self):
        """Initialize DynamoDB service"""
        self.region = os.getenv('AWS_REGION', 'us-east-1')
        self.users_table = os.getenv('DYNAMODB_USERS_TABLE', 'users')
        self.activity_table = os.getenv('DYNAMODB_ACTIVITY_TABLE', 'user_activity')
        self.endpoint_url = os.getenv('DYNAMODB_ENDPOINT_URL')  # For local DynamoDB
        
        # Initialize aioboto3 session
        self.session = aioboto3.Session()
        
        # Log configuration
        target = "Local DynamoDB" if self.endpoint_url else "AWS DynamoDB"
        logger.info(f"DynamoDB service initialized - Target: {target}, Region: {self.region}, Users Table: {self.users_table}")
        if self.endpoint_url:
            logger.info(f"Using local DynamoDB endpoint: {self.endpoint_url}")
    
    def _get_dynamodb_kwargs(self):
        """Get DynamoDB connection parameters (local or AWS)"""
        kwargs = {'region_name': self.region}
        
        if self.endpoint_url:
            # Local DynamoDB configuration
            kwargs.update({
                'endpoint_url': self.endpoint_url,
                'aws_access_key_id': 'dummy',
                'aws_secret_access_key': 'dummy'
            })
        
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
            async with self.session.resource('dynamodb', **self._get_dynamodb_kwargs()) as dynamodb:
                table = await dynamodb.Table(self.users_table)
                
                response = await table.get_item(
                    Key={'user_id': user_id}
                )
                
                if 'Item' in response:
                    return UserProfile.from_dynamodb_item(response['Item'])
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
            async with self.session.resource('dynamodb', **self._get_dynamodb_kwargs()) as dynamodb:
                table = await dynamodb.Table(self.users_table)
                
                item = user_profile.to_dynamodb_item()
                
                # Use condition to prevent overwriting existing users
                await table.put_item(
                    Item=item,
                    ConditionExpression='attribute_not_exists(user_id)'
                )
                
                logger.info(f"Created user profile for {user_profile.user_id}")
                return user_profile
                
        except ClientError as e:
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                logger.warning(f"User profile already exists: {user_profile.user_id}")
                # Return existing profile
                existing_profile = await self.get_user_profile(user_profile.user_id)
                if existing_profile is None:
                    raise InternalServerError(f"User profile should exist but could not be retrieved: {user_profile.user_id}")
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
            user_profile.updated_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            
            async with self.session.resource('dynamodb', **self._get_dynamodb_kwargs()) as dynamodb:
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
            async with self.session.resource('dynamodb', **self._get_dynamodb_kwargs()) as dynamodb:
                table = await dynamodb.Table(self.users_table)
                
                await table.delete_item(
                    Key={'user_id': user_id}
                )
                
                logger.info(f"Deleted user profile for {user_id}")
                return True
                
        except ClientError as e:
            logger.error(f"DynamoDB error deleting user profile: {e}")
            raise InternalServerError(f"Failed to delete user profile: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error deleting user profile: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")
    
    async def sync_user_with_cognito(self, user_id: str, cognito_user_data: Dict[str, Any]) -> UserProfile:
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
            today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            current_time = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            
            async with self.session.resource('dynamodb', **self._get_dynamodb_kwargs()) as dynamodb:
                table = await dynamodb.Table(self.activity_table)
                
                # Use atomic updates to increment counters
                key = {'user_id': user_id, 'activity_date': today}
                
                if activity_type == 'chat':
                    await table.update_item(
                        Key=key,
                        UpdateExpression='ADD chat_messages :inc SET last_chat_at = :time',
                        ExpressionAttributeValues={
                            ':inc': 1,
                            ':time': current_time
                        }
                    )
                elif activity_type == 'login':
                    await table.update_item(
                        Key=key,
                        UpdateExpression='ADD login_count :inc SET last_login_at = :time',
                        ExpressionAttributeValues={
                            ':inc': 1,
                            ':time': current_time
                        }
                    )
                
                # Also update the user profile's conversation count if it's a chat
                if activity_type == 'chat':
                    users_table = await dynamodb.Table(self.users_table)
                    await users_table.update_item(
                        Key={'user_id': user_id},
                        UpdateExpression='ADD conversation_count :inc SET updated_at = :time',
                        ExpressionAttributeValues={
                            ':inc': 1,
                            ':time': current_time
                        }
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
            async with self.session.resource('dynamodb', **self._get_dynamodb_kwargs()) as dynamodb:
                table = await dynamodb.Table(self.users_table)
                
                # Query GSI on email
                response = await table.query(
                    IndexName='email-index',
                    KeyConditionExpression='email = :email',
                    ExpressionAttributeValues={':email': email}
                )
                
                if response['Items']:
                    return UserProfile.from_dynamodb_item(response['Items'][0])
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
            current_time = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            
            async with self.session.resource('dynamodb', **self._get_dynamodb_kwargs()) as dynamodb:
                table = await dynamodb.Table(self.users_table)
                
                await table.update_item(
                    Key={'user_id': user_id},
                    UpdateExpression='SET last_login_at = :time, updated_at = :time',
                    ExpressionAttributeValues={':time': current_time}
                )
                
                # Also record login activity
                await self.record_user_activity(user_id, 'login')
                
        except Exception as e:
            logger.error(f"Error updating last login: {e}")
            # Don't raise error for login timestamp failures