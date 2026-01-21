# app/services/sns_service.py
import json
import logging
import os
from typing import Any, Dict, Optional

import boto3

logger = logging.getLogger(__name__)


class SNSService:
    def __init__(self):
        self.sns = boto3.client(
            "sns", region_name=os.getenv("AWS_SNS_REGION", "us-east-1")
        )
        self.topic_arn = os.getenv("SNS_TOPIC_ARN")
        # Support both platform-specific and generic ARNs
        self.android_app_arn = os.getenv("SNS_ANDROID_APP_ARN") or os.getenv(
            "SNS_PLATFORM_APP_ARN"
        )
        self.ios_app_arn = os.getenv("SNS_IOS_APP_ARN")

    def _get_platform_arn(self, platform: str) -> Optional[str]:
        """Get the appropriate Platform Application ARN based on platform."""
        if platform.lower() == "android":
            return self.android_app_arn
        if platform.lower() == "ios":
            return self.ios_app_arn
        return self.android_app_arn  # Fallback to android/generic

    def create_platform_endpoint(self, token: str, platform: str, user_id: str) -> str:
        """
        Creates a platform endpoint in AWS SNS.
        """
        platform_arn = self._get_platform_arn(platform)
        if not platform_arn:
            logger.error(f"No Platform Application ARN configured for {platform}")
            raise ValueError(f"Push notification platform {platform} is not configured")

        try:
            response = self.sns.create_platform_endpoint(
                PlatformApplicationArn=platform_arn, Token=token, CustomUserData=user_id
            )
            endpoint_arn = response["EndpointArn"]
            logger.info(f"Created SNS endpoint: {endpoint_arn} for user {user_id}")
            return endpoint_arn
        except Exception as e:
            logger.error(f"Failed to create SNS endpoint for user {user_id}: {e}")
            raise

    def subscribe_to_topic(self, endpoint_arn: str) -> str:
        """Subscribes an endpoint to the main SNS topic."""
        if not self.topic_arn:
            logger.warning("No SNS_TOPIC_ARN configured, skipping subscription")
            return ""

        try:
            response = self.sns.subscribe(
                TopicArn=self.topic_arn, Protocol="application", Endpoint=endpoint_arn
            )
            return response["SubscriptionArn"]
        except Exception as e:
            logger.error(f"Failed to subscribe {endpoint_arn} to topic: {e}")
            raise

    def _generate_payload(
        self, title: str, body: str, data: Optional[Dict[str, Any]] = None
    ) -> str:
        """Generates a cross-platform JSON payload for SNS."""
        data = data or {}

        gcm_payload = {
            "notification": {
                "title": title,
                "body": body,
                "sound": "default",
                "click_action": "fcm.ACTION.HELLO",
            },
            "data": data,
            "priority": "high",
        }

        apns_payload = {
            "aps": {
                "alert": {"title": title, "body": body},
                "sound": "default",
                "mutable-content": 1,
            }
        }
        # Add custom data to APNS payload root
        apns_payload.update(data)

        message = {
            "default": body,
            "GCM": json.dumps(gcm_payload),
            "APNS": json.dumps(apns_payload),
        }
        return json.dumps(message)

    def publish_to_topic(
        self, title: str, body: str, data: Optional[Dict[str, Any]] = None
    ):
        """Broadcasts a notification to all subscribers of the topic."""
        if not self.topic_arn:
            logger.error("Attempted to publish to topic but SNS_TOPIC_ARN is missing")
            return None

        try:
            payload = self._generate_payload(title, body, data)
            response = self.sns.publish(
                TopicArn=self.topic_arn, Message=payload, MessageStructure="json"
            )
            logger.info(
                f"Broadcasted message {response['MessageId']} to topic {self.topic_arn}"
            )
            return response["MessageId"]
        except Exception as e:
            logger.error(f"Failed to publish to topic: {e}")
            return None

    def publish_to_endpoint(
        self,
        endpoint_arn: str,
        title: str,
        body: str,
        data: Optional[Dict[str, Any]] = None,
    ):
        """Sends a direct notification to a specific device endpoint."""
        try:
            payload = self._generate_payload(title, body, data)
            response = self.sns.publish(
                TargetArn=endpoint_arn, Message=payload, MessageStructure="json"
            )
            logger.info(
                f"Sent direct message {response['MessageId']} to "
                f"endpoint {endpoint_arn}"
            )
            return response["MessageId"]
        except Exception as e:
            # Handle disabled endpoints (token invalidated by FCM/APNs)
            if "EndpointDisabled" in str(e):
                logger.warning(
                    f"Endpoint {endpoint_arn} is disabled. Should be cleaned up."
                )
            else:
                logger.error(f"Failed to publish to endpoint {endpoint_arn}: {e}")
            return None

    def delete_platform_endpoint(self, endpoint_arn: str):
        """Deletes a platform endpoint from AWS SNS."""
        try:
            self.sns.delete_endpoint(EndpointArn=endpoint_arn)
            logger.info(f"Deleted SNS endpoint: {endpoint_arn}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete SNS endpoint {endpoint_arn}: {e}")
            return False
