# app/services/sns_service.py
import asyncio
import json
import logging
import os
import warnings
from typing import Any, Dict, Optional

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)


def _warn_sync_sns_call(method_name: str) -> None:
    """Emit a DeprecationWarning when an SNS sync method is called.

    The sync wrappers blocking-call boto3 from inside async contexts —
    silently stalling the event loop. Each has an `*_async` counterpart
    that wraps the same call in `asyncio.to_thread`. This warning makes
    accidental sync use visible in CI / dev logs without breaking
    production behavior.
    """
    warnings.warn(
        f"SNSService.{method_name} is sync and blocks the event loop. "
        f"Call SNSService.{method_name}_async() from async contexts.",
        DeprecationWarning,
        stacklevel=3,
    )


class SNSService:
    def __init__(self):
        # The boto3 SNS client is built lazily (see `sns` property) — its
        # construction resolves credentials/endpoints and is a measurable
        # cold-start cost. Routers instantiate SNSService at import, but most
        # requests never publish, so defer it to first use.
        self._sns = None
        self.topic_arn = os.getenv("SNS_TOPIC_ARN")
        # Support both platform-specific and generic ARNs
        self.android_app_arn = os.getenv("SNS_ANDROID_APP_ARN") or os.getenv(
            "SNS_PLATFORM_APP_ARN"
        )
        self.ios_app_arn = os.getenv("SNS_IOS_APP_ARN")

    @property
    def sns(self) -> Any:
        """Lazily build (and cache) the tuned boto3 SNS client on first use.

        - max_pool_connections=50 prevents pool exhaustion under push bursts.
        - retries=adaptive backs off intelligently on SNS throttling.
        """
        if self._sns is None:
            self._sns = boto3.client(
                "sns",
                region_name=os.getenv("AWS_SNS_REGION", "us-east-1"),
                config=Config(
                    max_pool_connections=50,
                    retries={"max_attempts": 5, "mode": "adaptive"},
                ),
            )
        return self._sns

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
        _warn_sync_sns_call("create_platform_endpoint")
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
        _warn_sync_sns_call("subscribe_to_topic")
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
        _warn_sync_sns_call("publish_to_topic")
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
        _warn_sync_sns_call("publish_to_endpoint")
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
        _warn_sync_sns_call("delete_platform_endpoint")
        try:
            self.sns.delete_endpoint(EndpointArn=endpoint_arn)
            logger.info(f"Deleted SNS endpoint: {endpoint_arn}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete SNS endpoint {endpoint_arn}: {e}")
            return False

    # ------------------------------------------------------------------
    # Async variants
    # ------------------------------------------------------------------
    # The sync methods above remain in place because they're called from a
    # background APScheduler thread (see services/scheduler.py) where the
    # event loop is not running. The async variants below delegate to the
    # sync implementations via asyncio.to_thread so async callers (FastAPI
    # routes) can opt-in to non-blocking SNS calls without spawning their
    # own threads. Once all callers migrate, the sync wrappers can become
    # thin proxies or be removed.

    async def create_platform_endpoint_async(
        self, token: str, platform: str, user_id: str
    ) -> str:
        """Async variant of create_platform_endpoint (offloads to threadpool)."""
        return await asyncio.to_thread(
            self.create_platform_endpoint, token, platform, user_id
        )

    async def subscribe_to_topic_async(self, endpoint_arn: str) -> str:
        """Async variant of subscribe_to_topic (offloads to threadpool)."""
        return await asyncio.to_thread(self.subscribe_to_topic, endpoint_arn)

    async def publish_to_topic_async(
        self, title: str, body: str, data: Optional[Dict[str, Any]] = None
    ):
        """Async variant of publish_to_topic (offloads to threadpool)."""
        return await asyncio.to_thread(self.publish_to_topic, title, body, data)

    async def publish_to_endpoint_async(
        self,
        endpoint_arn: str,
        title: str,
        body: str,
        data: Optional[Dict[str, Any]] = None,
    ):
        """Async variant of publish_to_endpoint (offloads to threadpool)."""
        return await asyncio.to_thread(
            self.publish_to_endpoint, endpoint_arn, title, body, data
        )

    async def delete_platform_endpoint_async(self, endpoint_arn: str):
        """Async variant of delete_platform_endpoint (offloads to threadpool)."""
        return await asyncio.to_thread(self.delete_platform_endpoint, endpoint_arn)
