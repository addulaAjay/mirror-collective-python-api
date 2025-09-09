#!/usr/bin/env python3
"""
Performance analysis script for enhanced chat endpoint
Identifies actual bottlenecks before optimization
"""

import asyncio
import json
import logging
import sys
import time

import requests

# Configure logging to see performance analysis
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)


def test_api_performance():
    """Test the actual API endpoint to see real performance"""

    print("🔍 REAL API BOTTLENECK ANALYSIS")
    print("=" * 50)

    # Test data
    test_requests = [
        {
            "name": "New Conversation Test",
            "payload": {
                "message": "Hello, this is a test message for performance analysis.",
                "user_id": "test_user_perf",
                "user_name": "Performance Test User",
                "create_new_conversation": True,
            },
        },
        {
            "name": "Existing Conversation Test",
            "payload": {
                "message": "This is a follow-up message in the same conversation.",
                "user_id": "test_user_perf",
                "user_name": "Performance Test User",
                "conversation_id": "will_be_set_from_first_request",
            },
        },
    ]

    # Check if server is running
    try:
        response = requests.get("http://localhost:8000/health", timeout=5)
        if response.status_code != 200:
            print("❌ Server not running. Please start the server first:")
            print("   uvicorn src.app.handler:app --reload --port 8000")
            return
    except requests.exceptions.ConnectionError:
        print("❌ Cannot connect to server. Please start the server first:")
        print("   uvicorn src.app.handler:app --reload --port 8000")
        return

    conversation_id = None

    for i, test_case in enumerate(test_requests, 1):
        print(f"\n🧪 Test {i}: {test_case['name']}")
        print("-" * 40)

        payload = test_case["payload"].copy()

        # Use conversation ID from previous request if available
        if conversation_id and "conversation_id" in payload:
            payload["conversation_id"] = conversation_id

        # Measure request time
        start_time = time.time()

        try:
            response = requests.post(
                "http://localhost:8000/api/chat/enhanced",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )

            request_time = time.time() - start_time

            if response.status_code == 200:
                data = response.json()
                conversation_id = data.get("conversation_id")  # Save for next request

                print(f"✅ SUCCESS")
                print(f"   Total time: {request_time:.3f}s")
                print(f"   Response length: {len(data.get('reply', ''))} chars")
                print(f"   Conversation ID: {conversation_id}")

                # Look for performance logs in response
                if request_time > 3.0:
                    print(f"⚠️  SLOW RESPONSE: {request_time:.3f}s > 3s")
                elif request_time > 1.0:
                    print(f"⚡ MODERATE: {request_time:.3f}s")
                else:
                    print(f"🚀 FAST: {request_time:.3f}s")

            else:
                print(f"❌ FAILED: HTTP {response.status_code}")
                print(f"   Response: {response.text}")
                print(f"   Time: {request_time:.3f}s")

        except requests.exceptions.Timeout:
            request_time = time.time() - start_time
            print(f"⏰ TIMEOUT after {request_time:.3f}s")
        except Exception as e:
            request_time = time.time() - start_time
            print(f"💥 ERROR after {request_time:.3f}s: {e}")

    print(f"\n💡 ANALYSIS COMPLETE")
    print("Check the server logs to see detailed performance breakdown.")
    print("Look for '🔍 PERFORMANCE ANALYSIS' logs to identify bottlenecks.")


def test_without_server():
    """Test the use case directly without HTTP overhead"""
    print("🔍 DIRECT USE CASE ANALYSIS (No HTTP)")
    print("=" * 50)
    print("ℹ️  This requires mocking external services (OpenAI, DynamoDB)")
    print("ℹ️  For real analysis, start the server and use API test above.")


def main():
    print("📊 Enhanced Chat Performance Analysis")
    print("=" * 50)

    # First try API test
    test_api_performance()

    print("\n" + "=" * 50)
    print("� NEXT STEPS:")
    print("1. Check server logs for detailed timing breakdown")
    print("2. Look for the operation taking the most time")
    print("3. Common bottlenecks:")
    print("   - 🤖 OpenAI API calls (usually 2-5 seconds)")
    print("   - 💾 Database operations (should be <100ms each)")
    print("   - 🔄 Multiple sequential operations")
    print("4. Focus optimization on the slowest operation first")


if __name__ == "__main__":
    main()
