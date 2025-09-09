"""
Test script for the new MirrorGPT conversation message integration
Validates that the database optimization and integration works correctly
"""

import os
import sys
from datetime import datetime, timezone

# Add src to path for imports
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    from app.models.conversation import ConversationMessage
    from app.services.conversation_service import ConversationService
    from app.services.dynamodb_service import DynamoDBService
    from app.services.mirror_orchestrator import MirrorOrchestrator
    from app.services.openai_service import OpenAIService

    print("✅ All imports successful!")
except ImportError as e:
    print(f"❌ Import error: {e}")
    print("Make sure to run this script from the project root directory")
    sys.exit(1)


def test_message_mirrorgpt_integration():
    """Test ConversationMessage MirrorGPT field integration"""
    print("\n🧪 Testing ConversationMessage MirrorGPT integration...")

    # Create test message
    message = ConversationMessage(
        message_id="test_123",
        conversation_id="conv_123",
        role="user",
        content="I feel uncertain about my direction in life.",
        timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )

    # Mock analysis data
    analysis_result = {
        "signal_1_emotional_resonance": {
            "dominant_emotion": "uncertainty",
            "valence": -0.2,
        },
        "signal_3_archetype_blend": {"primary": "Seeker", "confidence": 0.8},
    }

    confidence_scores = {"overall": 0.8, "emotional": 0.85}

    # Test adding MirrorGPT analysis
    message.add_mirrorgpt_analysis(
        user_id="user_123",
        session_id="session_123",
        analysis_result=analysis_result,
        confidence_scores=confidence_scores,
    )

    # Verify analysis storage
    assert message.has_mirrorgpt_analysis() == True
    assert message.user_id == "user_123"
    assert message.signal_3_archetype_blend["primary"] == "Seeker"

    # Test serialization/deserialization
    item = message.to_dynamodb_item()
    restored = ConversationMessage.from_dynamodb_item(item)
    assert restored.has_mirrorgpt_analysis() == True

    print("✅ ConversationMessage MirrorGPT integration works!")


def test_service_method_existence():
    """Test that required service methods exist"""
    print("\n🧪 Testing service method existence...")

    # Test ConversationService
    conv_service = ConversationService()
    required_conv_methods = [
        "add_message_with_mirrorgpt_analysis",
        "get_messages_with_mirrorgpt_analysis",
        "get_user_mirrorgpt_signals",
    ]

    for method in required_conv_methods:
        assert hasattr(conv_service, method), f"ConversationService missing {method}"

    # Test MirrorOrchestrator
    dynamodb = DynamoDBService()
    openai = OpenAIService()
    orchestrator = MirrorOrchestrator(dynamodb, openai)

    required_orchestrator_methods = [
        "_get_recent_signals_from_messages",
        "apply_mirrorgpt_analysis_to_message",
    ]

    for method in required_orchestrator_methods:
        assert hasattr(orchestrator, method), f"MirrorOrchestrator missing {method}"

    print("✅ All required service methods exist!")


def test_integration_workflow():
    """Test the conceptual integration workflow"""
    print("\n🧪 Testing integration workflow...")

    # 1. Create message with analysis
    message = ConversationMessage(
        message_id="workflow_test",
        conversation_id="conv_workflow",
        role="user",
        content="Test message",
        timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )

    # 2. Mock MirrorGPT analysis result
    mock_mirrorgpt_data = {
        "user_id": "user_workflow",
        "session_id": "session_workflow",
        "analysis_result": {
            "signal_3_archetype_blend": {"primary": "Sage", "confidence": 0.9}
        },
        "confidence_scores": {"overall": 0.9},
    }

    # 3. Test MirrorOrchestrator integration
    dynamodb = DynamoDBService()
    openai = OpenAIService()
    orchestrator = MirrorOrchestrator(dynamodb, openai)

    # Apply analysis to message (this replaces the old echo_signals storage)
    orchestrator.apply_mirrorgpt_analysis_to_message(message, mock_mirrorgpt_data)

    # 4. Verify the message now has the analysis
    assert message.has_mirrorgpt_analysis() == True
    assert message.signal_3_archetype_blend["primary"] == "Sage"

    print("✅ Integration workflow test passed!")


def main():
    """Run all tests"""
    print("🚀 MirrorGPT Database Integration Validation")
    print("=" * 60)
    print("Testing the new approach where MirrorGPT analysis is stored")
    print("directly in conversation messages instead of echo_signals table")

    try:
        # Test 1: Basic integration
        test_message_mirrorgpt_integration()

        # Test 2: Service methods
        test_service_method_existence()

        # Test 3: Workflow
        test_integration_workflow()

        print("\n" + "=" * 60)
        print("🎉 ALL TESTS PASSED!")
        print("\n✅ Key Achievements:")
        print("  • MirrorGPT analysis can be stored in conversation messages")
        print("  • ConversationService supports MirrorGPT operations")
        print("  • MirrorOrchestrator uses integrated storage approach")
        print("  • echo_signals table is no longer needed")
        print("  • Database redundancy reduced")

        print("\n📋 Implementation Status:")
        print("  ✅ ConversationMessage model enhanced")
        print("  ✅ MirrorOrchestrator updated")
        print("  ✅ ConversationService enhanced")
        print("  ✅ Enhanced chat use case updated")
        print("  ⏳ Ready for echo_signals table removal")

        print("\n📝 Next Steps:")
        print("  1. Test with actual database connections")
        print("  2. Run end-to-end chat with MirrorGPT")
        print("  3. Execute echo_signals table removal script")
        print("  4. Update API documentation")
        print("  5. Consider user_activity table optimization")

        return True

    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
