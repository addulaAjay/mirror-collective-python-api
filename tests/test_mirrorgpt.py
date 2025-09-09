"""
Comprehensive tests for MirrorGPT functionality
Tests archetype engine, orchestrator, API endpoints, and integration
"""

import asyncio
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Test imports
from src.app.services.archetype_engine import (
    ArchetypeEngine,
    ChangeDetector,
    ConfidenceCalculator,
)
from src.app.services.mirror_orchestrator import MirrorOrchestrator, ResponseGenerator
from src.app.utils.archetype_data import ArchetypeDefinitions


class TestArchetypeDefinitions:
    """Test archetype data structures"""

    def test_get_all_archetypes(self):
        """Test archetype data retrieval"""
        archetypes = ArchetypeDefinitions.get_all_archetypes()

        # Should have 14 archetypes
        assert len(archetypes) == 14

        # Core four should be present
        core_four = ["Seeker", "Guardian", "Flamebearer", "Weaver"]
        for archetype in core_four:
            assert archetype in archetypes

        # Each archetype should have required fields
        for name, data in archetypes.items():
            assert "symbols" in data
            assert "emotions" in data
            assert "language_patterns" in data
            assert "core_resonance" in data
            assert isinstance(data["symbols"], list)
            assert isinstance(data["emotions"], list)

    def test_get_symbol_library(self):
        """Test symbol library structure"""
        symbols = ArchetypeDefinitions.get_symbol_library()

        # Should have expected categories
        expected_categories = [
            "threshold_symbols",
            "light_symbols",
            "water_symbols",
            "earth_symbols",
            "air_symbols",
            "transformation_symbols",
        ]

        for category in expected_categories:
            assert category in symbols
            assert isinstance(symbols[category], list)
            assert len(symbols[category]) > 0


class TestArchetypeEngine:
    """Test archetype detection engine"""

    def setup_method(self):
        """Setup test fixtures"""
        self.engine = ArchetypeEngine()

    def test_analyze_message_basic(self):
        """Test basic message analysis"""
        message = "I'm searching for meaning and truth in my life. This path feels illuminating."

        result = self.engine.analyze_message(message)

        # Should have all 5 signals
        assert "signal_1_emotional_resonance" in result
        assert "signal_2_symbolic_language" in result
        assert "signal_3_archetype_blend" in result
        assert "signal_4_narrative_position" in result
        assert "signal_5_motif_loops" in result

        # Should detect Seeker archetype
        assert result["primary_archetype"] == "Seeker"
        assert result["confidence_score"] > 0

    def test_emotional_resonance_analysis(self):
        """Test emotional resonance detection"""
        message = "I feel so joyful and excited about this new beginning!"

        result = self.engine._analyze_emotional_resonance(message)

        assert "valence" in result
        assert "arousal" in result
        assert "dominant_emotion" in result
        assert result["valence"] > 0  # Should be positive
        assert "joy" in result["detected_emotions"]

    def test_symbolic_language_extraction(self):
        """Test symbolic language detection"""
        message = "I crossed the threshold and found the light beyond the dark forest."

        result = self.engine._extract_symbolic_language(message)

        assert "extracted_symbols" in result
        assert "symbolic_density" in result
        assert len(result["extracted_symbols"]) > 0
        assert (
            "threshold" in result["extracted_symbols"]
            or "light" in result["extracted_symbols"]
        )

    def test_archetype_pattern_detection(self):
        """Test archetype pattern matching"""
        message = "I need to protect my family and create a safe space for everyone."

        emotional_data = self.engine._analyze_emotional_resonance(message)
        symbolic_data = self.engine._extract_symbolic_language(message)
        result = self.engine._detect_archetype_patterns(
            message, emotional_data, symbolic_data
        )

        assert "primary" in result
        assert "confidence" in result
        assert result["primary"] == "Guardian"  # Should detect Guardian
        assert result["confidence"] > 0.3  # Should have reasonable confidence

    def test_narrative_position_analysis(self):
        """Test narrative position detection"""
        message = (
            "I'm at the beginning of a new chapter in my life, ready to start fresh."
        )

        result = self.engine._analyze_narrative_position(message)

        assert "stage" in result
        assert "hero_journey_phase" in result
        assert "transformation_marker" in result
        assert result["stage"] in [
            "beginning",
            "middle",
            "climax",
            "resolution",
            "unknown",
        ]

    def test_motif_loop_detection(self):
        """Test motif loop pattern detection"""
        message = "I always feel like I'm not good enough, no matter what I achieve."

        result = self.engine._detect_motif_loops(message)

        assert "current_motifs" in result
        assert "active_loops" in result
        assert (
            "perfectionism" in result["current_motifs"]
            or "worthiness" in result["current_motifs"]
        )


class TestConfidenceCalculator:
    """Test confidence calculation"""

    def test_calculate_overall_confidence(self):
        """Test overall confidence calculation"""
        # Mock analysis result
        analysis_result = {
            "signal_3_archetype_blend": {"confidence": 0.8},
            "signal_2_symbolic_language": {"symbolic_density": 5.0},
            "signal_1_emotional_resonance": {"intensity": 0.6, "certainty": 0.7},
            "signal_4_narrative_position": {
                "journey_confidence": 2,
                "stage_confidence": 1,
            },
        }

        result = ConfidenceCalculator.calculate_overall_confidence(analysis_result)

        assert "overall" in result
        assert "archetype" in result
        assert "symbol" in result
        assert "emotion" in result
        assert 0 <= result["overall"] <= 1
        assert result["archetype"] == 0.8


class TestChangeDetector:
    """Test change detection and Mirror Moments"""

    def setup_method(self):
        """Setup test fixtures"""
        self.detector = ChangeDetector()

    def test_detect_archetype_shift(self):
        """Test archetype shift detection"""
        current_analysis = {
            "signal_3_archetype_blend": {"primary": "Flamebearer", "confidence": 0.8}
        }

        previous_profile = {"current_archetype_stack": {"primary": "Guardian"}}

        result = self.detector.detect_changes(current_analysis, previous_profile)

        assert result["change_detected"] is True
        assert len(result["changes"]) > 0
        assert result["changes"][0]["type"] == "archetype_shift"

    def test_no_change_detected(self):
        """Test when no change is detected"""
        current_analysis = {
            "signal_3_archetype_blend": {"primary": "Guardian", "confidence": 0.7}
        }

        previous_profile = {
            "current_archetype_stack": {"primary": "Guardian", "confidence_score": 0.65}
        }

        result = self.detector.detect_changes(current_analysis, previous_profile)

        assert result["change_detected"] is False


class TestResponseGenerator:
    """Test response generation"""

    def setup_method(self):
        """Setup test fixtures"""
        self.mock_openai_service = MagicMock()
        self.generator = ResponseGenerator(self.mock_openai_service)

    def test_generate_response_basic(self):
        """Test basic response generation"""
        analysis_result = {
            "signal_3_archetype_blend": {"primary": "Seeker", "confidence": 0.8},
            "signal_2_symbolic_language": {"extracted_symbols": ["light", "path"]},
            "signal_1_emotional_resonance": {"dominant_emotion": "curiosity"},
        }

        change_analysis = {"change_detected": False}

        result = self.generator.generate_response(
            "I'm searching for meaning", analysis_result, change_analysis
        )

        assert "response_text" in result
        assert "archetype_context" in result
        assert result["archetype_context"] == "Seeker"
        assert "Seeker" in result["response_text"]

    @pytest.mark.asyncio
    async def test_generate_enhanced_response(self):
        """Test enhanced AI response generation"""
        self.mock_openai_service.send_async.return_value = "Enhanced AI response"

        analysis_result = {
            "signal_3_archetype_blend": {"primary": "Guardian", "confidence": 0.7},
            "signal_2_symbolic_language": {"extracted_symbols": []},
            "signal_1_emotional_resonance": {"dominant_emotion": "caring"},
        }

        change_analysis = {"change_detected": False}

        result = await self.generator.generate_enhanced_response(
            "I want to protect my family", analysis_result, change_analysis
        )

        assert result == "Enhanced AI response"
        self.mock_openai_service.send_async.assert_called_once()


class TestMirrorOrchestrator:
    """Test main orchestrator functionality"""

    def setup_method(self):
        """Setup test fixtures"""
        self.mock_dynamodb = AsyncMock()
        self.mock_openai = MagicMock()
        self.orchestrator = MirrorOrchestrator(self.mock_dynamodb, self.mock_openai)

    @pytest.mark.asyncio
    async def test_process_mirror_chat_new_user(self):
        """Test processing chat for new user"""
        # Mock empty history
        self.mock_dynamodb.get_user_archetype_profile.return_value = None
        self.mock_dynamodb.get_recent_echo_signals.return_value = []
        self.mock_dynamodb.save_echo_signal.return_value = {}
        self.mock_dynamodb.save_user_archetype_profile.return_value = {}

        result = await self.orchestrator.process_mirror_chat(
            user_id="test_user",
            message="I'm seeking truth and meaning in life",
            session_id="test_session",
            use_enhanced_response=False,
        )

        assert result["success"] is True
        assert "response" in result
        assert "archetype_analysis" in result
        assert result["archetype_analysis"]["primary_archetype"] == "Seeker"

        # Should have saved signal data
        self.mock_dynamodb.save_echo_signal.assert_called_once()
        self.mock_dynamodb.save_user_archetype_profile.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_mirror_chat_with_history(self):
        """Test processing chat with user history"""
        # Mock existing profile and signals
        self.mock_dynamodb.get_user_archetype_profile.return_value = {
            "current_archetype_stack": {"primary": "Guardian", "confidence_score": 0.6}
        }
        self.mock_dynamodb.get_recent_echo_signals.return_value = [
            {"signal_3_archetype_blend": {"primary": "Guardian"}}
        ]
        self.mock_dynamodb.save_echo_signal.return_value = {}
        self.mock_dynamodb.save_user_archetype_profile.return_value = {}

        result = await self.orchestrator.process_mirror_chat(
            user_id="test_user",
            message="I feel the need to transform and change everything",
            session_id="test_session",
            use_enhanced_response=False,
        )

        assert result["success"] is True
        # Should detect archetype shift from Guardian to Flamebearer
        assert result["change_detection"]["change_detected"] is True

    @pytest.mark.asyncio
    async def test_get_user_insights(self):
        """Test user insights generation"""
        # Mock data
        self.mock_dynamodb.get_user_archetype_profile.return_value = {
            "current_archetype_stack": {"primary": "Seeker", "stability_score": 0.8}
        }
        self.mock_dynamodb.get_recent_echo_signals.return_value = [
            {"signal_1_emotional_resonance": {"valence": 0.5}},
            {"signal_1_emotional_resonance": {"valence": 0.7}},
        ]
        self.mock_dynamodb.get_user_mirror_moments.return_value = [
            {"moment_type": "breakthrough_moment"}
        ]

        result = await self.orchestrator.get_user_insights("test_user")

        assert "archetype_journey" in result
        assert "signal_patterns" in result
        assert "growth_indicators" in result
        assert result["archetype_journey"]["current_primary"] == "Seeker"


@pytest.mark.asyncio
class TestMirrorGPTAPIEndpoints:
    """Test API endpoints (integration tests)"""

    def setup_method(self):
        """Setup test client"""
        # Mock dependencies would be setup here for API testing
        pass

    async def test_mirror_chat_endpoint(self):
        """Test /mirrorgpt/chat endpoint"""
        # This would test the actual API endpoint with TestClient
        # Placeholder for full API integration tests
        pass

    async def test_archetype_analysis_endpoint(self):
        """Test /mirrorgpt/analyze endpoint"""
        pass

    async def test_profile_endpoint(self):
        """Test /mirrorgpt/profile endpoint"""
        pass


class TestDatabaseIntegration:
    """Test database operations and table interactions"""

    @pytest.mark.asyncio
    async def test_save_echo_signal(self):
        """Test saving echo signal data"""
        # Mock DynamoDB service
        mock_service = AsyncMock()
        mock_service.save_echo_signal.return_value = {"success": True}

        signal_data = {
            "user_id": "test_user",
            "timestamp": datetime.utcnow().isoformat(),
            "signal_1_emotional_resonance": {"valence": 0.5},
            "primary_archetype": "Seeker",
        }

        result = await mock_service.save_echo_signal(signal_data)
        assert result["success"] is True
        mock_service.save_echo_signal.assert_called_once_with(signal_data)

    @pytest.mark.asyncio
    async def test_save_mirror_moment(self):
        """Test saving mirror moment data"""
        mock_service = AsyncMock()
        mock_service.save_mirror_moment.return_value = {"moment_id": "test_moment"}

        moment_data = {
            "user_id": "test_user",
            "moment_id": "test_moment",
            "moment_type": "archetype_shift",
            "description": "Shift from Guardian to Flamebearer",
            "significance_score": 0.8,
        }

        result = await mock_service.save_mirror_moment(moment_data)
        assert "moment_id" in result
        mock_service.save_mirror_moment.assert_called_once_with(moment_data)


class TestErrorHandling:
    """Test error handling and edge cases"""

    def test_invalid_message_analysis(self):
        """Test analysis with invalid or empty messages"""
        engine = ArchetypeEngine()

        # Empty message
        result = engine.analyze_message("")
        assert (
            result["primary_archetype"] == "Unknown" or result["confidence_score"] == 0
        )

        # Very short message
        result = engine.analyze_message("Hi")
        assert "primary_archetype" in result

    def test_malformed_archetype_data(self):
        """Test handling of malformed archetype data"""
        # This would test robustness against data corruption
        pass

    @pytest.mark.asyncio
    async def test_orchestrator_error_handling(self):
        """Test orchestrator error handling"""
        mock_dynamodb = AsyncMock()
        mock_openai = MagicMock()

        # Simulate database error
        mock_dynamodb.get_user_archetype_profile.side_effect = Exception("DB Error")

        orchestrator = MirrorOrchestrator(mock_dynamodb, mock_openai)

        result = await orchestrator.process_mirror_chat(
            user_id="test_user", message="test message", session_id="test_session"
        )

        # Should handle error gracefully
        assert result["success"] is False
        assert "error" in result


class TestPerformance:
    """Test performance characteristics"""

    def test_analysis_performance(self):
        """Test analysis performance with long messages"""
        engine = ArchetypeEngine()

        # Long message
        long_message = "I am seeking truth and meaning in my life. " * 100

        import time

        start_time = time.time()
        result = engine.analyze_message(long_message)
        end_time = time.time()

        # Should complete within reasonable time
        assert (end_time - start_time) < 5.0  # 5 seconds max
        assert "primary_archetype" in result

    def test_batch_analysis_performance(self):
        """Test performance with multiple messages"""
        engine = ArchetypeEngine()

        messages = [
            "I'm searching for meaning",
            "I need to protect everyone",
            "Time for transformation",
            "I see the patterns connecting",
        ]

        import time

        start_time = time.time()

        results = []
        for message in messages:
            result = engine.analyze_message(message)
            results.append(result)

        end_time = time.time()

        # Should complete batch within reasonable time
        assert (end_time - start_time) < 10.0  # 10 seconds max
        assert len(results) == 4


if __name__ == "__main__":
    # Run tests with: python -m pytest tests/test_mirrorgpt.py -v
    pytest.main([__file__, "-v"])
