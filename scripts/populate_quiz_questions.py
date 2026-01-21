import asyncio
import json
import os
import sys
from decimal import Decimal

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from src.app.services.dynamodb_service import DynamoDBService  # noqa: E402


def load_questions():
    """Load questions from local JSON file"""
    json_path = os.path.join(
        os.path.dirname(__file__),
        "../../mirror_collective_app/MirrorCollectiveApp/src/assets/questions.json",
    )

    # Adjust path if needed based on actual location relative to this script
    # This assumes the script is run from mirror_collective_python_api root
    # and the app is at ../mirror_collective_app

    # Let's try to find the file dynamically if the hardcoded one fails
    if not os.path.exists(json_path):
        print(f"Path not found: {json_path}")
        # Try a different relative path assuming running from python api root
        json_path = (
            "../../mirror_collective_app/MirrorCollectiveApp/src/assets/questions.json"
        )

    if not os.path.exists(json_path):
        print(f"Could not find questions.json at {json_path}")
        return None

    with open(json_path, "r") as f:
        data = json.load(f, parse_float=Decimal)
        return data.get("questions", [])


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
