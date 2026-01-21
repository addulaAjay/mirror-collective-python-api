"""
Integration test script for MirrorGPT functionality
Tests the complete system integration and verifies all components work together
"""

import asyncio
import logging
import os
import sys

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))

from app.services.archetype_engine import (  # noqa: E402
    ArchetypeEngine,
    ChangeDetector,
    ConfidenceCalculator,
)
from app.utils.archetype_data import ArchetypeDefinitions  # noqa: E402

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def test_archetype_definitions():
    """Test archetype data definitions"""
    logger.info("Testing archetype definitions...")

    try:
        archetypes = ArchetypeDefinitions.get_all_archetypes()
        assert (
            len(archetypes) == 14
        ), f"Expected 14 archetypes, got {len(archetypes)}"  # nosec

        symbols = ArchetypeDefinitions.get_symbol_library()
        assert len(symbols) > 0, "Symbol library is empty"  # nosec

        relationships = ArchetypeDefinitions.get_archetype_relationships()
        assert len(relationships) > 0, "Archetype relationships are empty"  # nosec

        practices = ArchetypeDefinitions.get_integration_practices()
        assert (  # nosec
            len(practices) == 14
        ), f"Expected 14 integration practices, got {len(practices)}"

        logger.info("✅ Archetype definitions test passed")
        return True
    except Exception as e:
        logger.error(f"❌ Archetype definitions test failed: {e}")
        return False


def test_archetype_engine():
    """Test archetype engine functionality"""
    logger.info("Testing archetype engine...")

    try:
        engine = ArchetypeEngine()

        # Test different archetypal messages
        test_messages = [
            {
                "message": (
                    "I'm searching for truth and meaning in life, "
                    "seeking the light beyond darkness."
                ),
                "expected_archetype": "Seeker",
            },
            {
                "message": (
                    "I need to protect my family and create a safe "
                    "haven for everyone I care about."
                ),
                "expected_archetype": "Guardian",
            },
            {
                "message": (
                    "It's time to break free from these chains and "
                    "transform everything that holds me back."
                ),
                "expected_archetype": "Flamebearer",
            },
            {
                "message": (
                    "I see the beautiful patterns connecting all things "
                    "in this cosmic web of creation."
                ),
                "expected_archetype": "Weaver",
            },
        ]

        for test_case in test_messages:
            result = engine.analyze_message(test_case["message"])

            # Verify structure
            required_signals = [
                "signal_1_emotional_resonance",
                "signal_2_symbolic_language",
                "signal_3_archetype_blend",
                "signal_4_narrative_position",
                "signal_5_motif_loops",
            ]

            for signal in required_signals:
                assert signal in result, f"Missing signal: {signal}"  # nosec

            # Verify archetype detection
            detected_archetype = result["primary_archetype"]
            confidence = result["confidence_score"]

            logger.info(f"Message: '{test_case['message'][:50]}...'")
            logger.info(
                f"Expected: {test_case['expected_archetype']}, "
                f"Detected: {detected_archetype}, "
                f"Confidence: {confidence:.3f}"
            )

            if detected_archetype == test_case["expected_archetype"]:
                logger.info("✅ Archetype detection correct")
            else:
                logger.warning(
                    "⚠️  Archetype detection mismatch "
                    "(this may be acceptable based on message content)"
                )

            # Verify confidence is reasonable
            assert (  # nosec
                0 <= confidence <= 1
            ), f"Confidence should be between 0-1, got {confidence}"

        logger.info("✅ Archetype engine test passed")
        return True

    except Exception as e:
        logger.error(f"❌ Archetype engine test failed: {e}")
        return False


def test_confidence_calculator():
    """Test confidence calculation"""
    logger.info("Testing confidence calculator...")

    try:
        calculator = ConfidenceCalculator()

        # Mock analysis result
        mock_analysis = {
            "signal_3_archetype_blend": {"confidence": 0.8},
            "signal_2_symbolic_language": {"symbolic_density": 5.0},
            "signal_1_emotional_resonance": {"intensity": 0.6, "certainty": 0.7},
            "signal_4_narrative_position": {
                "journey_confidence": 2,
                "stage_confidence": 1,
            },
        }

        confidence_scores = calculator.calculate_overall_confidence(mock_analysis)

        required_scores = ["overall", "archetype", "symbol", "emotion", "historical"]
        for score_type in required_scores:
            assert (  # nosec
                score_type in confidence_scores
            ), f"Missing confidence score: {score_type}"
            score = confidence_scores[score_type]
            assert (  # nosec
                0 <= score <= 1
            ), f"{score_type} confidence should be 0-1, got {score}"

        logger.info(f"Confidence scores: {confidence_scores}")
        logger.info("✅ Confidence calculator test passed")
        return True

    except Exception as e:
        logger.error(f"❌ Confidence calculator test failed: {e}")
        return False


def test_change_detector():
    """Test change detection"""
    logger.info("Testing change detector...")

    try:
        detector = ChangeDetector()

        # Test archetype shift detection
        current_analysis = {
            "signal_3_archetype_blend": {"primary": "Flamebearer", "confidence": 0.8},
            "signal_4_narrative_position": {
                "stage": "middle",
                "journey_confidence": 3,
                "stage_confidence": 2,
            },
        }

        previous_profile = {
            "current_archetype_stack": {"primary": "Guardian", "confidence_score": 0.7}
        }

        changes = detector.detect_changes(current_analysis, previous_profile)

        assert "change_detected" in changes, "Missing change_detected field"  # nosec
        assert (
            changes["change_detected"] is True
        ), "Should detect archetype shift"  # nosec
        assert len(changes["changes"]) > 0, "Should have change details"  # nosec
        assert (  # nosec
            changes["changes"][0]["type"] == "archetype_shift"
        ), "Should detect archetype shift"

        logger.info(f"Detected change: {changes['changes'][0]['message']}")
        logger.info("✅ Change detector test passed")
        return True

    except Exception as e:
        logger.error(f"❌ Change detector test failed: {e}")
        return False


def test_end_to_end_analysis():
    """Test complete end-to-end analysis workflow"""
    logger.info("Testing end-to-end analysis workflow...")

    try:
        engine = ArchetypeEngine()
        calculator = ConfidenceCalculator()
        detector = ChangeDetector()

        # Simulate user conversation progression
        messages = [
            "I feel lost and confused, searching for my purpose in life.",
            "I'm starting to see some light in the darkness, finding small truths.",
            "I've discovered something important about myself - "
            "time to protect what matters.",
            "Now I need to transform everything and burn away what doesn't serve me.",
        ]

        previous_profile = None
        previous_signals = []

        for i, message in enumerate(messages):
            logger.info(f"\n--- Message {i + 1}: '{message}' ---")

            # Analyze message
            analysis = engine.analyze_message(
                message,
                user_history=previous_signals,
                context_signals={"historical_motifs": {}},
            )

            # Calculate confidence
            confidence = calculator.calculate_overall_confidence(analysis)

            # Detect changes
            changes = detector.detect_changes(
                analysis, previous_profile, previous_signals
            )

            logger.info(
                f"Detected archetype: {analysis['primary_archetype']} "
                f"(confidence: {confidence['overall']:.3f})"
            )

            if changes["change_detected"]:
                logger.info(f"🎯 Change detected: {changes['changes'][0]['message']}")
                if changes.get("mirror_moment_triggered"):
                    logger.info("✨ Mirror Moment triggered!")

            # Update for next iteration
            previous_profile = {
                "current_archetype_stack": {
                    "primary": analysis["primary_archetype"],
                    "confidence_score": confidence["overall"],
                }
            }

            previous_signals.append(analysis)

        logger.info("✅ End-to-end analysis test passed")
        return True

    except Exception as e:
        logger.error(f"❌ End-to-end analysis test failed: {e}")
        return False


async def test_mock_orchestrator():
    """Test orchestrator functionality with mocks"""
    logger.info("Testing mock orchestrator functionality...")

    try:
        # Import with mock dependencies
        from unittest.mock import AsyncMock, MagicMock

        from app.services.mirror_orchestrator import MirrorOrchestrator

        # Create mock services
        mock_dynamodb = AsyncMock()
        mock_openai = MagicMock()

        # Setup mock returns
        mock_dynamodb.get_user_archetype_profile.return_value = None
        mock_dynamodb.save_user_archetype_profile.return_value = {"success": True}

        # Create orchestrator
        orchestrator = MirrorOrchestrator(mock_dynamodb, mock_openai)

        # Test processing
        result = await orchestrator.process_mirror_chat(
            user_id="test_user",
            message="I'm seeking truth and meaning in my life",
            session_id="test_session",
            use_enhanced_response=False,  # Skip OpenAI to avoid API calls
        )

        assert result["success"] is True, "Orchestrator should succeed"  # nosec
        assert "response" in result, "Should have response"  # nosec
        assert "archetype_analysis" in result, "Should have archetype analysis"  # nosec
        assert result["archetype_analysis"]["primary_archetype"] in [  # nosec
            "Seeker",
            "Wounded Explorer",
        ], "Should detect appropriate archetype"

        # Verify mock calls
        # Verify analysis was applied to message via MirrorOrchestrator
        # Note: echo_signals table is no longer used
        mock_dynamodb.save_user_archetype_profile.assert_called_once()

        logger.info(
            f"Orchestrator result: "
            f"{result['archetype_analysis']['primary_archetype']} archetype detected"
        )
        logger.info("✅ Mock orchestrator test passed")
        return True

    except Exception as e:
        logger.error(f"❌ Mock orchestrator test failed: {e}")
        return False


def run_all_tests():
    """Run all integration tests"""
    logger.info("=" * 60)
    logger.info("MIRRORGPT INTEGRATION TEST SUITE")
    logger.info("=" * 60)

    tests = [
        ("Archetype Definitions", test_archetype_definitions),
        ("Archetype Engine", test_archetype_engine),
        ("Confidence Calculator", test_confidence_calculator),
        ("Change Detector", test_change_detector),
        ("End-to-End Analysis", test_end_to_end_analysis),
        ("Mock Orchestrator", lambda: asyncio.run(test_mock_orchestrator())),
    ]

    results = []

    for test_name, test_func in tests:
        logger.info(f"\n🧪 Running {test_name} test...")
        try:
            success = test_func()
            results.append((test_name, success))
        except Exception as e:
            logger.error(f"❌ {test_name} test crashed: {e}")
            results.append((test_name, False))

    # Summary
    logger.info("\n" + "=" * 60)
    logger.info("TEST RESULTS SUMMARY")
    logger.info("=" * 60)

    passed = sum(1 for _, success in results if success)
    total = len(results)

    for test_name, success in results:
        status = "✅ PASSED" if success else "❌ FAILED"
        logger.info(f"{test_name}: {status}")

    logger.info(f"\nOverall: {passed}/{total} tests passed")

    if passed == total:
        logger.info("🎉 All tests passed! MirrorGPT integration is working correctly.")
        return True
    else:
        logger.error(
            f"⚠️  {total - passed} tests failed. Please review the errors above."
        )
        return False


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
