"""
Seed quiz questions to remote DynamoDB (staging/production)
Uses backend's local copy of questions.json (deployed with Lambda)
"""

import asyncio
import json
import os
import sys
from decimal import Decimal

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from src.app.services.dynamodb_service import DynamoDBService


def load_questions():
    """Load questions from backend's data directory"""
    json_path = os.path.join(
        os.path.dirname(__file__), "../src/app/data/questions.json"
    )

    if not os.path.exists(json_path):
        raise FileNotFoundError(
            f"❌ questions.json not found at {json_path}\n"
            f"💡 Copy from frontend:\n"
            f"   cp mirror_collective_app/MirrorCollectiveApp/assets/questions.json \\\n"
            f"      mirror_collective_python_api/src/app/data/questions.json"
        )

    print(f"✓ Loading questions from: {json_path}")
    with open(json_path, "r") as f:
        data = json.load(f, parse_float=Decimal)
        questions = data.get("questions", [])

        if not questions:
            raise ValueError("No questions found in questions.json")

        return questions


async def seed_remote_questions(stage="staging"):
    """Seed questions to remote DynamoDB environment"""

    questions = load_questions()
    print(f"📋 Loaded {len(questions)} questions from backend data")

    # Validate questions
    for q in questions:
        if not all(k in q for k in ["id", "question", "options", "type", "core"]):
            raise ValueError(f"Invalid question format: {q.get('id', 'unknown')}")

    # Connect to remote DynamoDB (uses AWS credentials from environment)
    dynamo_service = DynamoDBService()
    table_name = dynamo_service.quiz_questions_table

    print(f"🚀 Seeding to {table_name} ({stage} environment)")
    print(f"   Region: {os.getenv('AWS_REGION', 'us-east-1')}")

    # Use remote endpoint (not local)
    async with dynamo_service.session.resource("dynamodb") as dynamodb:
        table = await dynamodb.Table(table_name)

        for question in questions:
            q_id = question["id"]
            q_text = question["question"][:60]
            print(f"  ✓ Q{q_id}: {q_text}...")
            await table.put_item(Item=question)

    print(f"✅ Successfully seeded {len(questions)} questions to {stage}")
    print(f"   Table: {table_name}")
    return True


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else os.getenv("STAGE", "staging")

    print(f"\n{'='*60}")
    print(f"Quiz Questions Seeder - {stage.upper()}")
    print(f"{'='*60}\n")

    try:
        asyncio.run(seed_remote_questions(stage))
    except Exception as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)
