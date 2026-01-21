# src/app/services/scheduler.py
import logging
import os
import uuid
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler

from .sns_service import SNSService

logger = logging.getLogger(__name__)
sns_service = SNSService()


def push_job():
    """Scheduled job to send periodic test notifications"""
    random_str = str(uuid.uuid4())[:8]
    title = "Mirror Collective Reminder"
    body = f"Discover your daily archetype insights 🚀 {random_str}"

    try:
        msg_id = sns_service.publish_to_topic(title, body)
        if msg_id:
            logger.info(f"✅ Scheduled push sent: {msg_id}")
        else:
            logger.warning("❌ Scheduled push failed: No MessageId returned")
    except Exception as e:
        logger.error(f"❌ Error while publishing scheduled push: {e}")


def start_scheduler(interval_minutes: Optional[int] = None):
    """Initializes and starts the background task scheduler"""
    if interval_minutes is None:
        interval_minutes = int(os.getenv("AWS_SNS_INTERVAL", 60))

    scheduler = BackgroundScheduler()
    scheduler.add_job(push_job, "interval", minutes=interval_minutes)
    scheduler.start()
    logger.info(
        f"📢 Scheduler started. Sending push every {interval_minutes} minutes..."
    )
    return scheduler
