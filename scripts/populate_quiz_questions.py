import asyncio
import json
import os
import sys
from decimal import Decimal

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from src.app.services.dynamodb_service import DynamoDBService  # noqa: E402


def load_questions():
    """
    Load questions from backend's local data directory
    This copy is deployed with the Lambda function and seeded to DynamoDB
    """
    # Primary path: backend's data directory (works in all environments)
    json_path = os.path.join(
        os.path.dirname(__file__), "../src/app/data/questions.json"
    )

    # Fallback: try to load from frontend repo (local dev only)
    fallback_path = os.path.join(
        os.path.dirname(__file__),
        "../../mirror_collective_app/MirrorCollectiveApp/src/assets/questions.json",
    )

    # Try primary path first
    if os.path.exists(json_path):
        print(f"✓ Loading questions from backend data: {json_path}")
        with open(json_path, "r") as f:
            data = json.load(f, parse_float=Decimal)
            return data.get("questions", [])

    # Fallback to frontend (local dev convenience)
    elif os.path.exists(fallback_path):
        print(f"⚠️  Loading from frontend fallback: {fallback_path}")
        print(f"   Consider copying to: {json_path}")
        with open(fallback_path, "r") as f:
            data = json.load(f, parse_float=Decimal)
            return data.get("questions", [])

    # Neither path exists
    else:
        print(f"❌ questions.json not found!")
        print(f"   Expected: {json_path}")
        print(f"   Fallback: {fallback_path}")
        print(f"\n💡 Copy questions.json to backend data directory:")
        print(
            f"   cp mirror_collective_app/MirrorCollectiveApp/assets/questions.json \\"
        )
        print(f"      mirror_collective_python_api/src/app/data/questions.json")
        return None


async def populate_questions():
    """Populate DynamoDB with questions"""
    dynamo_service = DynamoDBService()
    questions = load_questions()

    if not questions:
        print("No questions found to populate.")
        return

    table_name = dynamo_service.quiz_questions_table
    print(f"Populating table: {table_name}")

    async with dynamo_service.session.resource(
        "dynamodb", **dynamo_service._get_dynamodb_kwargs()
    ) as dynamodb:
        table = await dynamodb.Table(table_name)

        for question in questions:
            print(f"Adding question {question['id']}...")
            await table.put_item(Item=question)

    print("Migration completed successfully!")


if __name__ == "__main__":
    asyncio.run(populate_questions())
