import asyncio
import os
import uuid

from app.services.dynamodb_service import DynamoDBService
from app.services.user_linking_service import UserLinkingService


async def verify_link_move():
    print("🚀 Starting UserLinkingService MOVE verification...")

    # Ensure local DynamoDB
    if not os.getenv("DYNAMODB_ENDPOINT_URL"):
        os.environ["DYNAMODB_ENDPOINT_URL"] = "http://localhost:8000"

    db = DynamoDBService()
    linker = UserLinkingService(db)

    anon_id = f"anon_{uuid.uuid4()}"
    user_id = f"user_{uuid.uuid4()}"

    print(f"Creating mock data for {anon_id}...")

    # 1. Create anonymous profile
    profile = {
        "user_id": anon_id,
        "archetype": {"name": "Visionary", "title": "The Dreamer"},
        "answers": [1, 2, 3],
    }
    await db.save_user_archetype_profile(profile)

    # 2. Create anonymous quiz result
    quiz_id = f"quiz_{anon_id}"
    quiz_result = {
        "quiz_id": quiz_id,
        "user_id": anon_id,
        "answers": [1, 2, 3],
        "archetype_result": {"name": "Visionary"},
    }
    await db.save_quiz_results(quiz_result)

    print("Data created. Performing link...")

    # Perform linking
    link_results = await linker.link_anonymous_data(anon_id, user_id)
    print(f"Link results: {link_results}")

    # Verify Deletion
    # Profile should be GONE from anon_id
    old_profile = await db.get_user_archetype_profile(anon_id)
    if old_profile is None:
        print("✅ SUCCESS: Anonymous profile DELETED after migration.")
    else:
        print("❌ FAILURE: Anonymous profile still exists!")

    # Profile should exist under user_id
    new_profile = await db.get_user_archetype_profile(user_id)
    if new_profile and new_profile.get("user_id") == user_id:
        print("✅ SUCCESS: Profile migrated to new user_id.")
    else:
        print("❌ FAILURE: Profile NOT found under new user_id!")

    # Quiz Result should be updated to user_id (but key quiz_id remains same)
    # Let's check all results for user_id
    user_quizzes = await db.get_user_quiz_results(user_id)
    matching_quiz = next((q for q in user_quizzes if q["quiz_id"] == quiz_id), None)

    if matching_quiz:
        print(f"✅ SUCCESS: Quiz result owner updated to {matching_quiz['user_id']}")
    else:
        print("❌ FAILURE: Quiz result not found for new user_id!")


if __name__ == "__main__":
    asyncio.run(verify_link_move())
