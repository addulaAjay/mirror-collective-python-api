"""
S3 service for uploading content to AWS S3.
Supports default bucket from env or explicit bucket per call.
Optional S3 endpoint override via S3_ENDPOINT_URL (e.g., for MinIO/localstack).
"""

import logging
import os
import time
import uuid
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class S3Service:
    def __init__(self):
        self.region = os.getenv("AWS_REGION", "us-east-1")
        self.endpoint_url = os.getenv("S3_ENDPOINT_URL")  # optional for non-AWS
        self.client = boto3.client(
            "s3",
            region_name=self.region,
            endpoint_url=self.endpoint_url,
        )

    def _resolve_bucket(self, bucket_name: Optional[str], content_type_hint: Optional[str]) -> str:
        """Resolve bucket from explicit param or env based on content type.

        Priority:
        1) bucket_name param
        2) S3_TEXT_BUCKET for text/*, S3_VOICE_BUCKET for audio/*, S3_VIDEO_BUCKET for video/*
        3) S3_BUCKET_NAME as final fallback
        """
        if bucket_name:
            return bucket_name

        ct = (content_type_hint or "").lower()
        if ct.startswith("text/"):
            bucket = os.getenv("S3_TEXT_BUCKET")
            if bucket:
                return bucket
        elif ct.startswith("audio/"):
            bucket = os.getenv("S3_VOICE_BUCKET")
            if bucket:
                return bucket
        elif ct.startswith("video/"):
            bucket = os.getenv("S3_VIDEO_BUCKET")
            if bucket:
                return bucket

        bucket = os.getenv("S3_BUCKET_NAME")
        if not bucket:
            raise ValueError(
                "No S3 bucket configured. Provide bucketName in the request or set one of: "
                "S3_TEXT_BUCKET, S3_VOICE_BUCKET, S3_VIDEO_BUCKET, or S3_BUCKET_NAME."
            )
        return bucket

    def _object_url(self, bucket: str, key: str) -> str:
        # Construct public-style URL (works for private too, just not directly readable without auth)
        if self.endpoint_url:
            # Custom endpoint (path-style assumed)
            base = self.endpoint_url.rstrip("/")
            return f"{base}/{bucket}/{key}"
        # AWS standard URL
        return f"https://{bucket}.s3.{self.region}.amazonaws.com/{key}"

    def upload_text(
        self,
        *,
        content: str,
        bucket_name: Optional[str] = None,
        key: Optional[str] = None,
        content_type: str = "text/plain",
        acl: Optional[str] = None,  # e.g., 'private' or 'public-read'
        metadata: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        bucket = self._resolve_bucket(bucket_name, content_type)
        if not key:
            ts = time.strftime("%Y%m%d-%H%M%S")
            key = f"uploads/{ts}-{uuid.uuid4().hex}.txt"

        put_kwargs: Dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "Body": content.encode("utf-8"),
            "ContentType": content_type,
        }
        if acl:
            put_kwargs["ACL"] = acl
        if metadata:
            put_kwargs["Metadata"] = metadata

        try:
            response = self.client.put_object(**put_kwargs)
            e_tag = response.get("ETag", "").strip('"')
            object_url = self._object_url(bucket, key)
            logger.info(f"Uploaded object to S3 - bucket={bucket}, key={key}, etag={e_tag}")

            return {
                "bucket": bucket,
                "key": key,
                "eTag": e_tag,
                "objectUrl": object_url,
                "contentType": content_type,
            }
        except ClientError as e:
            logger.error(f"S3 put_object failed: {e}")
            raise

    def upload_bytes(
        self,
        *,
        data: bytes,
        bucket_name: Optional[str] = None,
        key: Optional[str] = None,
        content_type: str = "application/octet-stream",
        acl: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        bucket = self._resolve_bucket(bucket_name, content_type)
        if not key:
            ts = time.strftime("%Y%m%d-%H%M%S")
            key = f"uploads/{ts}-{uuid.uuid4().hex}"

        put_kwargs: Dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "Body": data,
            "ContentType": content_type,
        }
        if acl:
            put_kwargs["ACL"] = acl
        if metadata:
            put_kwargs["Metadata"] = metadata

        try:
            response = self.client.put_object(**put_kwargs)
            e_tag = response.get("ETag", "").strip('"')
            object_url = self._object_url(bucket, key)
            return {
                "bucket": bucket,
                "key": key,
                "eTag": e_tag,
                "objectUrl": object_url,
                "contentType": content_type,
            }
        except ClientError as e:
            logger.error(f"S3 put_object (bytes) failed: {e}")
            raise

    def get_text(self, *, key: str, bucket_name: Optional[str] = None) -> Dict[str, Any]:
        """Fetch an S3 object and return decoded text with basic metadata."""
        bucket = self._resolve_bucket(bucket_name, None)
        try:
            obj = self.client.get_object(Bucket=bucket, Key=key)
            body = obj["Body"].read()
            content_type = obj.get("ContentType", "application/octet-stream")
            text = body.decode("utf-8", errors="replace")
            return {
                "bucket": bucket,
                "key": key,
                "content": text,
                "contentType": content_type,
                "contentLength": obj.get("ContentLength"),
                "lastModified": obj.get("LastModified").isoformat() if obj.get("LastModified") else None,
                "eTag": obj.get("ETag", "").strip('"'),
                "metadata": obj.get("Metadata", {}),
            }
        except ClientError as e:
            logger.error(f"S3 get_object failed: {e}")
            raise

    def generate_presigned_get_url(
        self, *, key: str, bucket_name: Optional[str] = None, expires_in: int = 900
    ) -> str:
        """Generate a pre-signed GET URL for the given object (default 15 minutes)."""
        bucket = self._resolve_bucket(bucket_name, None)
        try:
            url = self.client.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=expires_in,
            )
            return url
        except ClientError as e:
            logger.error(f"S3 generate_presigned_url failed: {e}")
            raise
