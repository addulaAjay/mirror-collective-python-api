"""
Core archetype detection and symbolic analysis engine
Implements the 5-signal analysis system for MirrorGPT
"""

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..utils.archetype_data import ArchetypeDefinitions

logger = logging.getLogger(__name__)


class ArchetypeEngine:
    """Core archetype detection and symbolic analysis engine"""

    def __init__(self):
        self.archetypes = ArchetypeDefinitions.get_all_archetypes()
        self.symbol_library = ArchetypeDefinitions.get_symbol_library()
        self.archetype_relationships = (
            ArchetypeDefinitions.get_archetype_relationships()
        )

    def analyze_message(
        self,
        message: str,
        user_history: Optional[List[Dict]] = None,
        context_signals: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Complete message analysis for archetype detection
        Returns all 5 signals + archetype classification
        """

        # Signal 1: Emotional Resonance Analysis
        emotional_resonance = self._analyze_emotional_resonance(message)

        # Signal 2: Symbolic Language Extraction
        symbolic_language = self._extract_symbolic_language(message)

        # Signal 3: Archetype Pattern Detection
        archetype_analysis = self._detect_archetype_patterns(
            message, emotional_resonance, symbolic_language
        )

        # Signal 4: Narrative Position Analysis
        narrative_position = self._analyze_narrative_position(message, user_history)

        # Signal 5: Motif Loop Detection
        motif_loops = self._detect_motif_loops(message, user_history, context_signals)

        return {
            "signal_1_emotional_resonance": emotional_resonance,
            "signal_2_symbolic_language": symbolic_language,
            "signal_3_archetype_blend": archetype_analysis,
            "signal_4_narrative_position": narrative_position,
            "signal_5_motif_loops": motif_loops,
            "primary_archetype": archetype_analysis["primary"],
            "confidence_score": archetype_analysis["confidence"],
            "timestamp": datetime.utcnow().isoformat(),
        }

    def _analyze_emotional_resonance(self, message: str) -> Dict[str, Any]:
        """Signal 1: Emotional resonance analysis"""

        # Emotion keyword mapping with weights
        emotion_patterns: Dict[str, Dict[str, Any]] = {
            "joy": {
                "pattern": r"\b(joy|happy|delight|elated|bliss|euphoria|celebration|glad|cheerful)\b",
                "weight": 1.2,
            },
            "sadness": {
                "pattern": r"\b(sad|grief|sorrow|mourn|loss|tears|heartbreak|melancholy|despair)\b",
                "weight": 1.0,
            },
            "anger": {
                "pattern": r"\b(angry|rage|fury|irritated|frustrated|mad|livid|outraged)\b",
                "weight": 1.3,
            },
            "fear": {
                "pattern": r"\b(fear|afraid|scared|terror|anxiety|worried|panic|dread)\b",
                "weight": 1.1,
            },
            "love": {
                "pattern": r"\b(love|adore|cherish|devotion|affection|tender|caring|compassion)\b",
                "weight": 1.5,
            },
            "curiosity": {
                "pattern": r"\b(curious|wonder|question|explore|seek|discover|intrigued)\b",
                "weight": 1.0,
            },
            "peace": {
                "pattern": r"\b(peace|calm|serene|tranquil|still|quiet|centered|balanced)\b",
                "weight": 1.0,
            },
            "excitement": {
                "pattern": r"\b(excited|thrilled|enthusiastic|energized|passionate|exhilarated)\b",
                "weight": 1.2,
            },
            "longing": {
                "pattern": r"\b(longing|yearning|craving|desire|aching|missing|wanting)\b",
                "weight": 1.1,
            },
            "shame": {
                "pattern": r"\b(shame|ashamed|embarrassed|guilty|humiliated|worthless)\b",
                "weight": 0.9,
            },
            "hope": {
                "pattern": r"\b(hope|hopeful|optimistic|faith|trust|belief|possibility)\b",
                "weight": 1.3,
            },
            "confusion": {
                "pattern": r"\b(confused|lost|unclear|uncertain|bewildered|puzzled)\b",
                "weight": 0.8,
            },
        }

        detected_emotions: Dict[str, float] = {}
        total_intensity: float = 0.0

        for emotion, config in emotion_patterns.items():
            pattern = str(config["pattern"])
            weight = float(config["weight"])
            matches = len(re.findall(pattern, message.lower()))
            if matches > 0:
                weighted_score = matches * weight
                detected_emotions[emotion] = weighted_score
                total_intensity += weighted_score

        # Calculate valence (-1 to 1)
        positive_emotions = ["joy", "love", "peace", "excitement", "hope", "curiosity"]
        negative_emotions = ["sadness", "anger", "fear", "shame", "confusion"]
        neutral_emotions = ["longing"]

        positive_score = sum(detected_emotions.get(e, 0) for e in positive_emotions)
        negative_score = sum(detected_emotions.get(e, 0) for e in negative_emotions)

        valence: float = 0.0
        if (positive_score + negative_score) > 0:
            valence = (positive_score - negative_score) / (
                positive_score + negative_score
            )

        # Calculate arousal (0 to 1) - intensity of emotions
        high_arousal = ["anger", "excitement", "fear", "rage", "panic"]
        low_arousal = ["peace", "sadness", "calm"]

        arousal_score = sum(
            detected_emotions.get(e, 0) for e in high_arousal if e in detected_emotions
        )
        low_arousal_score = sum(
            detected_emotions.get(e, 0) for e in low_arousal if e in detected_emotions
        )

        total_emotional_words = (
            len(message.split()) * 0.1
        )  # Normalize by message length
        arousal = min(arousal_score / max(total_emotional_words, 1), 1)

        # Dominant emotion
        dominant_emotion = "neutral"
        if detected_emotions:
            dominant_emotion = max(detected_emotions.items(), key=lambda x: x[1])[0]

        # Certainty score based on clarity of emotional expression
        certainty = min(total_intensity / max(len(message.split()) * 0.2, 1), 1)

        return {
            "valence": round(valence, 3),
            "arousal": round(arousal, 3),
            "certainty": round(certainty, 3),
            "intensity": round(total_intensity / max(len(message.split()), 1), 3),
            "dominant_emotion": dominant_emotion,
            "detected_emotions": {k: round(v, 2) for k, v in detected_emotions.items()},
        }

    def _extract_symbolic_language(self, message: str) -> Dict[str, Any]:
        """Signal 2: Symbolic language extraction"""

        extracted_symbols = []
        metaphor_types = []
        symbol_categories = {}

        # Check each symbol category
        for category, symbols in self.symbol_library.items():
            category_matches = []
            for symbol in symbols:
                # Use word boundaries and case-insensitive matching
                pattern = rf"\b{re.escape(symbol)}\b"
                if re.search(pattern, message.lower()):
                    extracted_symbols.append(symbol)
                    category_matches.append(symbol)

            if category_matches:
                symbol_categories[category] = category_matches

        # Detect metaphorical language patterns
        metaphor_indicators = [
            {
                "type": "simile",
                "pattern": r"\b(like|as if|reminds me of|feels like|seems like|appears to be)\b",
            },
            {
                "type": "metaphor",
                "pattern": r"\b(is|are|becomes?|transforms? into|turns? into)\b.*\b(symbol|represents?|embodies|means)\b",
            },
            {
                "type": "symbolic",
                "pattern": r"\b(symbolic|symbolizes|represents|stands for|signifies)\b",
            },
            {
                "type": "archetypal",
                "pattern": r"\b(archetype|pattern|theme|motif|recurring)\b",
            },
        ]

        for indicator in metaphor_indicators:
            if re.search(indicator["pattern"], message.lower()):
                metaphor_types.append(indicator["type"])

        # Advanced symbolic pattern detection
        symbolic_phrases = self._detect_symbolic_phrases(message)

        # Symbolic density (symbols per 100 words)
        word_count = len(message.split())
        symbolic_density = len(extracted_symbols) / max(word_count, 1) * 100

        return {
            "extracted_symbols": extracted_symbols,
            "symbol_categories": symbol_categories,
            "metaphor_types": list(set(metaphor_types)),  # Remove duplicates
            "symbolic_density": round(symbolic_density, 2),
            "symbolic_phrases": symbolic_phrases,
        }

    def _detect_symbolic_phrases(self, message: str) -> List[str]:
        """Detect complex symbolic expressions"""
        symbolic_phrases = []

        # Common symbolic constructions
        patterns = [
            r"\b(crossing|stepping through|walking into|entering) the \w+\b",
            r"\b(burning|breaking|shattering|dissolving) the \w+\b",
            r"\b(finding|discovering|uncovering|revealing) the \w+\b",
            r"\b(building|creating|weaving|crafting) the \w+\b",
            r"\b(mirror|reflection|shadow|echo) of \w+\b",
        ]

        for pattern in patterns:
            matches = re.findall(pattern, message.lower())
            symbolic_phrases.extend(matches)

        return symbolic_phrases[:5]  # Limit to most relevant

    def _detect_archetype_patterns(
        self, message: str, emotional_resonance: Dict, symbolic_language: Dict
    ) -> Dict[str, Any]:
        """Signal 3: Archetype pattern detection with enhanced scoring"""

        archetype_scores = {}

        for archetype_name, archetype_data in self.archetypes.items():
            score: float = 0.0
            match_details = {}

            # Symbol matching (40% weight)
            symbol_matches = 0
            matched_symbols = []
            for symbol in archetype_data["symbols"]:
                if symbol in symbolic_language["extracted_symbols"]:
                    symbol_matches += 1
                    matched_symbols.append(symbol)

            if len(archetype_data["symbols"]) > 0:
                symbol_score = (symbol_matches / len(archetype_data["symbols"])) * 0.4
                score += symbol_score
                match_details["symbols"] = {
                    "matches": symbol_matches,
                    "matched": matched_symbols,
                }

            # Emotion matching (30% weight)
            emotion_matches = 0
            matched_emotions = []
            for emotion in archetype_data["emotions"]:
                if emotion in emotional_resonance["detected_emotions"]:
                    emotion_weight = emotional_resonance["detected_emotions"][emotion]
                    emotion_matches += emotion_weight
                    matched_emotions.append(emotion)

            if len(archetype_data["emotions"]) > 0:
                emotion_score = (
                    min(emotion_matches / len(archetype_data["emotions"]), 1) * 0.3
                )
                score += emotion_score
                match_details["emotions"] = {
                    "score": emotion_matches,
                    "matched": matched_emotions,
                }

            # Language pattern matching (30% weight)
            language_matches = 0
            for pattern in archetype_data["language_patterns"]:
                matches = len(re.findall(pattern, message.lower()))
                language_matches += matches

            if len(archetype_data["language_patterns"]) > 0:
                language_score = (
                    min(language_matches / len(archetype_data["language_patterns"]), 1)
                    * 0.3
                )
                score += language_score
                match_details["language"] = {"matches": language_matches}

            archetype_scores[archetype_name] = {
                "score": round(score, 3),
                "details": match_details,
            }

        # Sort archetypes by score
        def get_score(item: Tuple[str, Dict[str, Any]]) -> float:
            return float(item[1].get("score", 0))

        sorted_archetypes = sorted(
            archetype_scores.items(), key=get_score, reverse=True
        )

        primary = sorted_archetypes[0][0] if sorted_archetypes else "Unknown"
        secondary = (
            sorted_archetypes[1][0]
            if len(sorted_archetypes) > 1 and float(sorted_archetypes[1][1].get("score", 0)) > 0.2  # type: ignore
            else None
        )
        tertiary = (
            sorted_archetypes[2][0]
            if len(sorted_archetypes) > 2 and float(sorted_archetypes[2][1].get("score", 0)) > 0.15  # type: ignore
            else None
        )

        primary_confidence = (
            sorted_archetypes[0][1]["score"] if sorted_archetypes else 0
        )

        return {
            "primary": primary,
            "secondary": secondary,
            "tertiary": tertiary,
            "confidence": primary_confidence,
            "blend_scores": {
                name: data["score"] for name, data in archetype_scores.items()
            },
            "match_details": archetype_scores.get(primary, {}).get("details", {}),
        }

    def _analyze_narrative_position(
        self, message: str, user_history: Optional[List[Dict]] = None
    ) -> Dict[str, Any]:
        """Signal 4: Narrative position analysis"""

        # Hero's journey phases with enhanced patterns
        hero_journey_patterns = {
            "ordinary_world": r"\b(normal|routine|everyday|usual|regular|stable|comfortable)\b",
            "call_to_adventure": r"\b(call|calling|invitation|opportunity|chance|beginning|stirring|awakening)\b",
            "refusal_of_call": r"\b(hesitat|resist|afraid|doubt|uncertain|not ready|avoiding|denial)\b",
            "meeting_mentor": r"\b(guide|teacher|mentor|wisdom|guidance|help|support|advice)\b",
            "crossing_threshold": r"\b(step|cross|enter|begin|start|commit|decide|leap|threshold)\b",
            "tests_allies_enemies": r"\b(challenge|test|friend|enemy|obstacle|support|help|ally|opponent)\b",
            "approach_inmost_cave": r"\b(deep|core|heart|center|fear|confront|face|prepare|gather)\b",
            "ordeal": r"\b(crisis|death|loss|breakdown|rock bottom|darkest|trial|suffering)\b",
            "reward": r"\b(gift|treasure|wisdom|insight|breakthrough|victory|achievement|realization)\b",
            "road_back": r"\b(return|integrate|apply|share|teach|give back|journey home)\b",
            "resurrection": r"\b(rebirth|transform|new|different|reborn|emerge|phoenix|renewal)\b",
            "return_with_elixir": r"\b(wisdom|healing|help others|serve|mastery|gift|medicine|teaching)\b",
        }

        # Narrative stages with enhanced detection
        narrative_stages = {
            "beginning": r"\b(start|begin|new|first|initial|opening|origin|inception|dawn)\b",
            "middle": r"\b(middle|during|process|journey|path|struggle|work|development|unfolding)\b",
            "climax": r"\b(climax|peak|crisis|turning point|breakthrough|moment|crescendo|culmination)\b",
            "resolution": r"\b(end|finish|complete|resolve|closure|peace|done|conclusion|fulfillment)\b",
        }

        # Detect journey phase
        detected_journey_phase = "unknown"
        highest_journey_score: float = 0.0

        for phase, pattern in hero_journey_patterns.items():
            matches = len(re.findall(pattern, message.lower()))
            if matches > highest_journey_score:
                highest_journey_score = matches
                detected_journey_phase = phase

        # Detect narrative stage
        detected_stage = "unknown"
        highest_stage_score: float = 0.0

        for stage, pattern in narrative_stages.items():
            matches = len(re.findall(pattern, message.lower()))
            if matches > highest_stage_score:
                highest_stage_score = matches
                detected_stage = stage

        # Transformation markers with enhanced detection
        transformation_indicators = [
            r"\b(transform|change|shift|evolve|grow|become|emerge|metamorphosis)\b",
            r"\b(different|new|rebirth|phoenix|butterfly|chrysalis|caterpillar)\b",
            r"\b(breakthrough|awakening|realization|enlightenment|epiphany)\b",
        ]

        transformation_marker = any(
            re.search(pattern, message.lower()) for pattern in transformation_indicators
        )

        # Analyze progression if history available
        progression_analysis = {}
        if user_history:
            progression_analysis = self._analyze_narrative_progression(user_history)

        return {
            "stage": detected_stage,
            "hero_journey_phase": detected_journey_phase,
            "transformation_marker": transformation_marker,
            "journey_confidence": highest_journey_score,
            "stage_confidence": highest_stage_score,
            "progression_analysis": progression_analysis,
        }

    def _analyze_narrative_progression(
        self, user_history: List[Dict]
    ) -> Dict[str, Any]:
        """Analyze narrative progression through user history"""
        if len(user_history) < 2:
            return {"trend": "insufficient_data"}

        # Extract stages from recent messages
        recent_stages = []
        for entry in user_history[-5:]:  # Last 5 entries
            stage = entry.get("signal_4_narrative_position", {}).get("stage", "unknown")
            recent_stages.append(stage)

        # Analyze progression
        stage_order = ["beginning", "middle", "climax", "resolution"]
        stage_indices = []

        for stage in recent_stages:
            if stage in stage_order:
                stage_indices.append(stage_order.index(stage))

        if len(stage_indices) >= 2:
            trend = (
                "progressing" if stage_indices[-1] > stage_indices[0] else "regressing"
            )
        else:
            trend = "stable"

        return {
            "trend": trend,
            "recent_stages": recent_stages,
            "stage_distribution": {
                stage: recent_stages.count(stage) for stage in stage_order
            },
        }

    def _detect_motif_loops(
        self,
        message: str,
        user_history: Optional[List[Dict]] = None,
        context_signals: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Signal 5: Motif loop detection with enhanced pattern recognition"""

        # Extract key themes/motifs from current message
        current_motifs = []

        # Enhanced psychological motifs with patterns
        motif_patterns = {
            "abandonment": r"\b(abandon|left|alone|desert|reject|isolat|forsak|betray)\b",
            "betrayal": r"\b(betray|trust|lie|deceiv|cheat|broken promise|dishonest|unfaithful)\b",
            "perfectionism": r"\b(perfect|flawless|never enough|not good enough|mistake|failure|inadequate)\b",
            "control": r"\b(control|manage|organize|plan|predict|certain|manipulat|dominat)\b",
            "approval": r"\b(approval|accept|like me|love me|validate|recognition|praise|acknowledgment)\b",
            "scarcity": r"\b(not enough|lack|scarce|limited|running out|shortage|insufficient)\b",
            "worthiness": r"\b(worthy|deserve|enough|valuable|matter|important|significant|valued)\b",
            "safety": r"\b(safe|secure|protected|danger|threat|risk|vulnerable|harm)\b",
            "freedom": r"\b(free|escape|trapped|cage|liberat|independ|autonomous|choice)\b",
            "belonging": r"\b(belong|fit in|outsider|different|home|family|community|included)\b",
            "power": r"\b(power|strength|weak|helpless|capable|competent|agency|influence)\b",
            "identity": r"\b(who am i|identity|self|authentic|real me|true self|persona)\b",
        }

        # Detect current motifs
        for motif, pattern in motif_patterns.items():
            if re.search(pattern, message.lower()):
                current_motifs.append(motif)

        # Initialize loop tracking variables
        active_loops = []
        new_loops = []
        broken_loops = []
        loop_strengths = {}

        # Analyze patterns if context available
        if context_signals and "historical_motifs" in context_signals:
            historical_motifs = context_signals["historical_motifs"]

            for motif in current_motifs:
                if motif in historical_motifs:
                    count = historical_motifs[motif].get("count", 0) + 1

                    # Determine loop status
                    if count >= 3:  # Loop threshold
                        active_loops.append(motif)
                        loop_strengths[motif] = min(
                            count / 10.0, 1.0
                        )  # Normalize strength
                    elif count == 2:
                        new_loops.append(motif)
                else:
                    # First occurrence
                    new_loops.append(motif)

            # Check for broken loops (motifs that appeared before but not now)
            for historical_motif, data in historical_motifs.items():
                if historical_motif not in current_motifs and data.get("count", 0) >= 3:
                    # Check if it was active recently
                    last_seen = data.get("last_seen", "")
                    if last_seen:  # If we have timing data, could check recency
                        broken_loops.append(historical_motif)

        # Calculate loop strength score
        loop_strength_score: float = 0.0
        if current_motifs:
            total_strength = sum(loop_strengths.values())
            loop_strength_score = total_strength / len(current_motifs)

        return {
            "current_motifs": current_motifs,
            "active_loops": active_loops,
            "new_loops_detected": new_loops,
            "broken_loops": broken_loops,
            "loop_strengths": loop_strengths,
            "loop_strength_score": round(loop_strength_score, 3),
        }


class ConfidenceCalculator:
    """Calculate confidence scores for archetype detection"""

    @staticmethod
    def calculate_overall_confidence(
        analysis_result: Dict[str, Any], historical_stability: float = 0.5
    ) -> Dict[str, float]:
        """Calculate confidence scores for all aspects of analysis"""

        # Archetype confidence
        archetype_confidence = analysis_result["signal_3_archetype_blend"]["confidence"]

        # Symbol confidence (based on symbolic density and matches)
        symbolic_data = analysis_result["signal_2_symbolic_language"]
        symbol_confidence = min(symbolic_data["symbolic_density"] / 10, 1.0)

        # Emotional confidence (based on emotion detection strength and certainty)
        emotional_data = analysis_result["signal_1_emotional_resonance"]
        emotion_confidence = min(
            (emotional_data["intensity"] * emotional_data["certainty"]) * 2, 1.0
        )

        # Narrative confidence (based on detection clarity)
        narrative_data = analysis_result["signal_4_narrative_position"]
        narrative_confidence = min(
            (narrative_data["journey_confidence"] + narrative_data["stage_confidence"])
            / 10,
            1.0,
        )

        # Overall confidence (weighted average)
        overall_confidence = (
            archetype_confidence * 0.35
            + symbol_confidence * 0.25
            + emotion_confidence * 0.25
            + narrative_confidence * 0.1
            + historical_stability * 0.05
        )

        return {
            "overall": round(overall_confidence, 3),
            "archetype": round(archetype_confidence, 3),
            "symbol": round(symbol_confidence, 3),
            "emotion": round(emotion_confidence, 3),
            "narrative": round(narrative_confidence, 3),
            "historical": round(historical_stability, 3),
        }


class ChangeDetector:
    """Detect archetype changes and Mirror Moments"""

    def __init__(self):
        self.confidence_threshold = 0.75
        self.significant_change_threshold = 0.3
        self.archetype_relationships = (
            ArchetypeDefinitions.get_archetype_relationships()
        )

    def detect_changes(
        self,
        current_analysis: Dict[str, Any],
        previous_profile: Optional[Dict[str, Any]] = None,
        previous_signals: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Detect all types of changes and Mirror Moments"""

        if not previous_profile:
            return {"change_detected": False, "reason": "no_previous_data"}

        changes_detected = []

        # 1. Archetype shift detection
        archetype_change = self._detect_archetype_shift(
            current_analysis, previous_profile
        )
        if archetype_change["detected"]:
            changes_detected.append(archetype_change)

        # 2. Confidence shift detection
        confidence_change = self._detect_confidence_shift(
            current_analysis, previous_profile
        )
        if confidence_change["detected"]:
            changes_detected.append(confidence_change)

        # 3. Loop transformation detection
        loop_change = self._detect_loop_transformation(
            current_analysis, previous_signals
        )
        if loop_change["detected"]:
            changes_detected.append(loop_change)

        # 4. Breakthrough/Integration detection
        breakthrough = self._detect_breakthrough_moment(
            current_analysis, previous_signals
        )
        if breakthrough["detected"]:
            changes_detected.append(breakthrough)

        return {
            "change_detected": len(changes_detected) > 0,
            "changes": changes_detected,
            "mirror_moment_triggered": any(
                c.get("is_mirror_moment", False) for c in changes_detected
            ),
        }

    def _detect_archetype_shift(self, current: Dict, previous: Dict) -> Dict[str, Any]:
        """Detect primary archetype changes"""

        prev_primary = previous.get("current_archetype_stack", {}).get("primary")
        curr_primary = current["signal_3_archetype_blend"]["primary"]
        curr_confidence = current["signal_3_archetype_blend"]["confidence"]

        if (
            prev_primary
            and prev_primary != curr_primary
            and curr_confidence > self.confidence_threshold
        ):
            significance = self._calculate_shift_significance(
                prev_primary, curr_primary
            )

            return {
                "detected": True,
                "type": "archetype_shift",
                "from_archetype": prev_primary,
                "to_archetype": curr_primary,
                "confidence": curr_confidence,
                "significance": significance,
                "is_mirror_moment": significance > 0.7,
                "message": f"Movement from {prev_primary} to {curr_primary} detected",
                "suggested_practice": ArchetypeDefinitions.get_integration_practices().get(
                    curr_primary, "Reflective journaling and integration"
                ),
            }

        return {"detected": False}

    def _detect_confidence_shift(self, current: Dict, previous: Dict) -> Dict[str, Any]:
        """Detect confidence level changes"""

        prev_confidence = float(
            previous.get("current_archetype_stack", {}).get("confidence_score", 0)
        )
        curr_confidence = float(current["signal_3_archetype_blend"]["confidence"])

        confidence_delta = abs(curr_confidence - prev_confidence)

        if confidence_delta > self.significant_change_threshold:
            direction = (
                "strengthening"
                if curr_confidence > prev_confidence
                else "destabilizing"
            )

            return {
                "detected": True,
                "type": "confidence_shift",
                "direction": direction,
                "delta": confidence_delta,
                "current_confidence": curr_confidence,
                "is_mirror_moment": confidence_delta > 0.5,
                "message": f"Archetype confidence {direction}: {confidence_delta:.2f} change",
            }

        return {"detected": False}

    def _detect_loop_transformation(
        self, current: Dict, previous_signals: Optional[List[Dict]] = None
    ) -> Dict[str, Any]:
        """Detect when pattern loops break or transform"""

        if not previous_signals:
            return {"detected": False}

        current_broken = current["signal_5_motif_loops"]["broken_loops"]

        if current_broken:
            return {
                "detected": True,
                "type": "loop_transformation",
                "broken_loops": current_broken,
                "is_mirror_moment": True,
                "message": f"Pattern loop(s) transformed: {', '.join(current_broken)}",
                "suggested_practice": "Integration and celebration practice",
            }

        return {"detected": False}

    def _detect_breakthrough_moment(
        self, current: Dict, previous_signals: Optional[List[Dict]] = None
    ) -> Dict[str, Any]:
        """Detect breakthrough or integration moments"""

        # Check for transformation markers in narrative position
        narrative = current["signal_4_narrative_position"]

        breakthrough_phases = ["reward", "resurrection", "return_with_elixir"]

        if (
            narrative["transformation_marker"]
            and narrative["hero_journey_phase"] in breakthrough_phases
        ):

            return {
                "detected": True,
                "type": "breakthrough_moment",
                "journey_phase": narrative["hero_journey_phase"],
                "is_mirror_moment": True,
                "message": "Breakthrough moment detected - transformation is integrating",
                "suggested_practice": "Integration and grounding practice",
            }

        return {"detected": False}

    def _calculate_shift_significance(
        self, from_archetype: str, to_archetype: str
    ) -> float:
        """Calculate significance of archetype shift"""

        # Check direct relationship
        direct = self.archetype_relationships.get((from_archetype, to_archetype))
        reverse = self.archetype_relationships.get((to_archetype, from_archetype))

        return (
            direct or reverse or 0.7
        )  # Default high significance for unmapped transitions
