# src/app/services/scheduler.py
from apscheduler.schedulers.background import BackgroundScheduler
import boto3, json, os, time
import json
import uuid

random_str = str(uuid.uuid4())[:8] 

sns_client = boto3.client(
    "sns",
    region_name=os.getenv("AWS_SNS_REGION"),
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
)

def push_job():
    topic_arn = os.getenv("SNS_TOPIC_ARN", "").strip()

    payload = {
        "default": "Hello from Scheduler!",
        "GCM": json.dumps({
            "notification": {"title": "Reminder", "body": f"Automated push 🚀 {random_str}"}
        }),
        "APNS": json.dumps({
            "aps": {"alert": f"Automated push 🚀 {random_str}", "sound": "default"}
        })
    }

    try:
        resp = sns_client.publish(
            TopicArn=topic_arn,
            Message=json.dumps(payload),
            MessageStructure="json"
        )
        print("✅ Sent:", resp["MessageId"])
    except Exception as e:
        print("❌ Error while publishing:", e)

def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(push_job, "interval", minutes=int(os.getenv("AWS_SNS_INTERVAL")))  # type: ignore
    scheduler.start()
    print(f"📢 Scheduler started. Sending push every {os.getenv('AWS_SNS_INTERVAL')} minutes...")
    return scheduler
