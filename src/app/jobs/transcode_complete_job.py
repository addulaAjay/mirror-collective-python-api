"""
MediaConvert completion handler.

Triggered by an EventBridge rule on "MediaConvert Job State Change" events with
status COMPLETE. The job (submitted in EchoService._submit_video_transcode)
carries echo_id + attachment_id in UserMetadata, and the event lists the output
file path(s). We resolve the .mp4 rendition and write it onto the attachment as
``playable_url`` so the share viewer can play the video in any browser.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from ..services.echo_service import get_echo_service

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _s3_uri_to_canonical(uri: str, region: str, bucket: str) -> Optional[str]:
    """s3://bucket/key  ->  https://bucket.s3.<region>.amazonaws.com/key."""
    if not uri or not uri.startswith("s3://"):
        return None
    without = uri[len("s3://") :]
    parts = without.split("/", 1)
    if len(parts) != 2:
        return None
    b, key = parts
    return f"https://{b}.s3.{region}.amazonaws.com/{key}"


def _extract_mp4_path(detail: Dict[str, Any]) -> Optional[str]:
    """Pull the first .mp4 output file path from a MediaConvert COMPLETE event."""
    groups: List[Dict[str, Any]] = detail.get("outputGroupDetails", []) or []
    for g in groups:
        for out in g.get("outputDetails", []) or []:
            for path in out.get("outputFilePaths", []) or []:
                if isinstance(path, str) and path.lower().endswith(".mp4"):
                    return path
    return None


async def _handle(event: Dict[str, Any]) -> Dict[str, Any]:
    detail = event.get("detail", {}) or {}
    status = detail.get("status")
    meta = detail.get("userMetadata", {}) or {}
    echo_id = meta.get("echo_id")
    attachment_id = meta.get("attachment_id")

    if status != "COMPLETE":
        logger.info(f"Ignoring MediaConvert event with status={status}")
        return {"success": True, "skipped": f"status={status}"}
    if not echo_id or not attachment_id:
        logger.warning("MediaConvert event missing echo_id/attachment_id metadata")
        return {"success": False, "error": "missing metadata"}

    mp4_uri = _extract_mp4_path(detail)
    if not mp4_uri:
        logger.warning(f"No .mp4 output in event for echo {echo_id}")
        return {"success": False, "error": "no mp4 output"}

    svc = get_echo_service()
    playable_url = _s3_uri_to_canonical(mp4_uri, svc.region, svc.s3_bucket) or mp4_uri
    ok = await svc.set_attachment_playable_url(echo_id, attachment_id, playable_url)
    return {"success": ok, "echo_id": echo_id, "attachment_id": attachment_id}


def lambda_handler(event: Dict, context: Any) -> Dict:
    """EventBridge entry point for MediaConvert job-completion events."""
    logger.info(f"Transcode-complete event: {event.get('detail-type', 'unknown')}")
    result = asyncio.run(_handle(event))
    return {"statusCode": 200 if result.get("success") else 500, "body": result}
