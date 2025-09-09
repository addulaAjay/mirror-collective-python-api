# app/services/sns_service.py
import boto3
import json
import os

class SNSService:
    def __init__(self):
        self.sns = boto3.client("sns", region_name=os.getenv("AWS_SNS_REGION"))
        self.topic_arn = os.getenv("SNS_TOPIC_ARN")
        self.platform_app_arn = os.getenv("SNS_PLATFORM_APP_ARN")

    def create_platform_endpoint(self, fcm_token: str, user_id: str) -> str:
        response = self.sns.create_platform_endpoint(
            PlatformApplicationArn=self.platform_app_arn,
            Token=fcm_token,
            CustomUserData=user_id
        )
        return response["EndpointArn"]

    def subscribe_to_topic(self, endpoint_arn: str) -> str:
        response = self.sns.subscribe(
            TopicArn=self.topic_arn,
            Protocol="application",
            Endpoint=endpoint_arn
        )
        return response["SubscriptionArn"]

    def publish_to_topic(self, title: str, body: str):
        gcm_payload = {
            "notification": {"title": title, "body": body, "sound": "default"},
            "priority": "high",
            "android_channel_id": "general"
        }
        message = {"default": body, "GCM": json.dumps(gcm_payload)}

        response = self.sns.publish(
            TopicArn=self.topic_arn,
            Message=json.dumps(message),
            MessageStructure="json"
        )
        return response["MessageId"]

    def publish_to_endpoint(self, endpoint_arn: str, title: str, body: str):
        gcm_payload = {
            "notification": {"title": title, "body": body, "sound": "default"},
            "priority": "high",
            "android_channel_id": "general"
        }
        message = {"default": body, "GCM": json.dumps(gcm_payload)}

        response = self.sns.publish(
            TargetArn=endpoint_arn,
            Message=json.dumps(message),
            MessageStructure="json"
        )
        return response["MessageId"]
