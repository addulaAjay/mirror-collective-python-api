"""
Core archetype detection and symbolic analysis engine
Implements the 5-signal analysis system for MirrorGPT
"""

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
                "pattern": (
                    r"\b(joy|happy|delight|elated|bliss|euphoria|"
                    r"celebration|glad|cheerful)\b"
                ),
                "weight": 1.2,
            },
            "sadness": {
                "pattern": (
                    r"\b(sad|grief|sorrow|mourn|loss|tears|heartbreak|"
                    r"melancholy|despair)\b"
                ),
                "weight": 1.0,
            },
            "anger": {
                "pattern": (
                    r"\b(angry|rage|fury|irritated|frustrated|mad|" r"livid|outraged)\b"
                ),
                "weight": 1.3,
            },
            "fear": {
                "pattern": (
                    r"\b(fear|afraid|scared|terror|anxiety|worried|panic|" r"dread)\b"
                ),
                "weight": 1.1,
            },
            "love": {
                "pattern": (
                    r"\b(love|adore|cherish|devotion|affection|tender|"
                    r"caring|compassion)\b"
                ),
                "weight": 1.5,
            },
            "curiosity": {
                "pattern": (
                    r"\b(curious|wonder|question|explore|seek|discover|" r"intrigued)\b"
                ),
                "weight": 1.0,
            },
            "peace": {
                "pattern": (
                    r"\b(peace|calm|serene|tranquil|still|quiet|centered|"
                    r"balanced)\b"
                ),
                "weight": 1.0,
            },
            "excitement": {
                "pattern": (
                    r"\b(excited|thrilled|enthusiastic|energized|"
                    r"passionate|exhilarated)\b"
                ),
                "weight": 1.2,
            },
            "longing": {
                "pattern": (
                    r"\b(longing|yearning|craving|desire|aching|missing|" r"wanting)\b"
                ),
                "weight": 1.1,
            },
            "shame": {
                "pattern": (
                    r"\b(shame|ashamed|embarrassed|guilty|humiliated|" r"worthless)\b"
                ),
                "weight": 0.9,
            },
            "hope": {
                "pattern": (
                    r"\b(hope|hopeful|optimistic|faith|trust|belief|" r"possibility)\b"
                ),
                "weight": 1.3,
            },
            "confusion": {
                "pattern": (
                    r"\b(confused|lost|unclear|uncertain|bewildered|" r"puzzled)\b"
                ),
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

        positive_score = sum(detected_emotions.get(e, 0) for e in positive_emotions)
        negative_score = sum(detected_emotions.get(e, 0) for e in negative_emotions)

        valence: float = 0.0
        if (positive_score + negative_score) > 0:
            valence = (positive_score - negative_score) / (
                positive_score + negative_score
            )

        # Calculate arousal (0 to 1) - intensity of emotions
        high_arousal = ["anger", "excitement", "fear", "rage", "panic"]

        arousal_score = sum(
            detected_emotions.get(e, 0) for e in high_arousal if e in detected_emotions
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
                "pattern": (
                    r"\b(like|as if|reminds me of|feels like|seems like|"
                    r"appears to be)\b"
                ),
            },
            {
                "type": "metaphor",
                "pattern": (
                    r"\b(is|are|becomes?|transforms? into|turns? into)\b.*"
                    r"\b(symbol|represents?|embodies|means)\b"
                ),
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

        archetype_scores: Dict[str, Dict[str, Any]] = {}

        for archetype_name, archetype_data in self.archetypes.items():
            # Scoring components
            symbol_results = self._score_symbols(archetype_data, symbolic_language)
            emotion_results = self._score_emotions(archetype_data, emotional_resonance)
            language_results = self._score_language(archetype_data, message)

            score_sum = symbol_results["score"] + emotion_results["score"]
            score_sum += language_results["score"]
            score: float = float(score_sum)

            archetype_scores[archetype_name] = {
                "score": round(score, 3),
                "details": {
                    "symbols": symbol_results["details"],
                    "emotions": emotion_results["details"],
                    "language": language_results["details"],
                },
            }

        # Sort archetypes by score
        def get_score(item: Tuple[str, Dict[str, Any]]) -> float:
            score = item[1].get("score", 0.0)
            if score is None:
                return 0.0
            return float(score)

        sorted_archetypes = sorted(
            archetype_scores.items(), key=get_score, reverse=True
        )

        primary = sorted_archetypes[0][0] if sorted_archetypes else "Unknown"
        if len(sorted_archetypes) > 1:
            score2_val = sorted_archetypes[1][1].get("score", 0.0)
            score2 = float(score2_val) if score2_val is not None else 0.0
            secondary = sorted_archetypes[1][0] if score2 > 0.2 else None
        else:
            secondary = None
        if len(sorted_archetypes) > 2:
            score3_val = sorted_archetypes[2][1].get("score", 0.0)
            score3 = float(score3_val) if score3_val is not None else 0.0
            tertiary = sorted_archetypes[2][0] if score3 > 0.15 else None
        else:
            tertiary = None

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

    def _score_symbols(self, archetype_data: Dict, symbolic_language: Dict) -> Dict:
        """Calculate symbol match score (40% weight)"""
        matches = 0
        matched = []
        for symbol in archetype_data["symbols"]:
            if symbol in symbolic_language["extracted_symbols"]:
                matches += 1
                matched.append(symbol)

        score = 0.0
        if archetype_data["symbols"]:
            score = (matches / len(archetype_data["symbols"])) * 0.4

        return {
            "score": score,
            "details": {"matches": matches, "matched": matched},
        }

    def _score_emotions(self, archetype_data: Dict, emotional_resonance: Dict) -> Dict:
        """Calculate emotion match score (30% weight)"""
        matches = 0.0
        matched = []
        for emotion in archetype_data["emotions"]:
            if emotion in emotional_resonance["detected_emotions"]:
                weight = emotional_resonance["detected_emotions"][emotion]
                matches += weight
                matched.append(emotion)

        score = 0.0
        if archetype_data["emotions"]:
            score = min(matches / len(archetype_data["emotions"]), 1) * 0.3

        return {
            "score": score,
            "details": {"score": matches, "matched": matched},
        }

    def _score_language(self, archetype_data: Dict, message: str) -> Dict:
        """Calculate language pattern match score (30% weight)"""
        matches = 0
        for pattern in archetype_data["language_patterns"]:
            matches += len(re.findall(pattern, message.lower()))

        score = 0.0
        if archetype_data["language_patterns"]:
            score = min(matches / len(archetype_data["language_patterns"]), 1) * 0.3

        return {
            "score": score,
            "details": {"matches": matches},
        }

    def _analyze_narrative_position(
        self, message: str, user_history: Optional[List[Dict]] = None
    ) -> Dict[str, Any]:
        """Signal 4: Narrative position analysis"""

        # Hero's journey phases with enhanced patterns
        hero_journey_patterns = {
            "ordinary_world": (
                r"\b(normal|routine|everyday|usual|regular|stable|" r"comfortable)\b"
            ),
            "call_to_adventure": (
                r"\b(call|calling|invitation|opportunity|chance|beginning|"
                r"stirring|awakening)\b"
            ),
            "refusal_of_call": (
                r"\b(hesitat|resist|afraid|doubt|uncertain|not ready|"
                r"avoiding|denial)\b"
            ),
            "meeting_mentor": (
                r"\b(guide|teacher|mentor|wisdom|guidance|help|support|" r"advice)\b"
            ),
            "crossing_threshold": (
                r"\b(step|cross|enter|begin|start|commit|decide|leap|" r"threshold)\b"
            ),
            "tests_allies_enemies": (
                r"\b(challenge|test|friend|enemy|obstacle|support|help|"
                r"ally|opponent)\b"
            ),
            "approach_inmost_cave": (
                r"\b(deep|core|heart|center|fear|confront|face|prepare|" r"gather)\b"
            ),
            "ordeal": (
                r"\b(crisis|death|loss|breakdown|rock bottom|darkest|trial|"
                r"suffering)\b"
            ),
            "reward": (
                r"\b(gift|treasure|wisdom|insight|breakthrough|victory|"
                r"achievement|realization)\b"
            ),
            "road_back": (
                r"\b(return|integrate|apply|share|teach|give back|" r"journey home)\b"
            ),
            "resurrection": (
                r"\b(rebirth|transform|new|different|reborn|emerge|"
                r"phoenix|renewal)\b"
            ),
            "return_with_elixir": (
                r"\b(wisdom|healing|help others|serve|mastery|gift|"
                r"medicine|teaching)\b"
            ),
        }

        # Narrative stages with enhanced detection
        narrative_stages = {
            "beginning": (
                r"\b(start|begin|new|first|initial|opening|origin|" r"inception|dawn)\b"
            ),
            "middle": (
                r"\b(middle|during|process|journey|path|struggle|work|"
                r"development|unfolding)\b"
            ),
            "climax": (
                r"\b(climax|peak|crisis|turning point|breakthrough|moment|"
                r"crescendo|culmination)\b"
            ),
            "resolution": (
                r"\b(end|finish|complete|resolve|closure|peace|done|"
                r"conclusion|fulfillment)\b"
            ),
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
        # 1. Extract current motifs
        current_motifs = self._extract_current_motifs(message)

        # 2. Analyze loops if context available
        loop_results = self._analyze_motif_loops(current_motifs, context_signals)

        return {
            "current_motifs": current_motifs,
            "active_loops": loop_results["active_loops"],
            "new_loops_detected": loop_results["new_loops"],
            "broken_loops": loop_results["broken_loops"],
            "loop_strengths": loop_results["loop_strengths"],
            "loop_strength_score": round(loop_results["strength_score"], 3),
        }

    def _extract_current_motifs(self, message: str) -> List[str]:
        """Identify psychological motifs present in the message"""
        motif_patterns = {
            "abandonment": (
                r"\b(abandon|left|alone|desert|reject|isolat|forsak|" r"betray)\b"
            ),
            "betrayal": (
                r"\b(betray|trust|lie|deceiv|cheat|broken promise|"
                r"dishonest|unfaithful)\b"
            ),
            "perfectionism": (
                r"\b(perfect|flawless|never enough|not good enough|"
                r"mistake|failure|inadequate)\b"
            ),
            "control": (
                r"\b(control|manage|organize|plan|predict|certain|"
                r"manipulat|dominat)\b"
            ),
            "approval": (
                r"\b(approval|accept|like me|love me|validate|"
                r"recognition|praise|acknowledgment)\b"
            ),
            "scarcity": (
                r"\b(not enough|lack|scarce|limited|running out|"
                r"shortage|insufficient)\b"
            ),
            "worthiness": (
                r"\b(worthy|deserve|enough|valuable|matter|important|"
                r"significant|valued)\b"
            ),
            "safety": r"\b(safe|secure|protected|danger|threat|risk|vulnerable|harm)\b",
            "freedom": (
                r"\b(free|escape|trapped|cage|liberat|independ|" r"autonomous|choice)\b"
            ),
            "belonging": (
                r"\b(belong|fit in|outsider|different|home|family|"
                r"community|included)\b"
            ),
            "power": (
                r"\b(power|strength|weak|helpless|capable|competent|"
                r"agency|influence)\b"
            ),
            "identity": (
                r"\b(who am i|identity|self|authentic|real me|" r"true self|persona)\b"
            ),
        }
        detected = []
        for motif, pattern in motif_patterns.items():
            if re.search(pattern, message.lower()):
                detected.append(motif)
        return detected

    def _analyze_motif_loops(self, current_motifs, context_signals):
        """Track recurrence and dissolution of motifs over time"""
        active_loops = []
        new_loops = []
        broken_loops = []
        loop_strengths = {}

        if context_signals and "historical_motifs" in context_signals:
            hist = context_signals["historical_motifs"]
            for motif in current_motifs:
                if motif in hist:
                    count = hist[motif].get("count", 0) + 1
                    if count >= 3:
                        active_loops.append(motif)
                        loop_strengths[motif] = min(count / 10.0, 1.0)
                    else:
                        new_loops.append(motif)
                else:
                    new_loops.append(motif)

            for motif, data in hist.items():
                if motif not in current_motifs and data.get("count", 0) >= 3:
                    broken_loops.append(motif)
        else:
            new_loops = current_motifs

        strength_score = 0.0
        if current_motifs:
            strength_score = sum(loop_strengths.values()) / len(current_motifs)

        return {
            "active_loops": active_loops,
            "new_loops": new_loops,
            "broken_loops": broken_loops,
            "loop_strengths": loop_strengths,
            "strength_score": strength_score,
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
        n_conf = narrative_data["journey_confidence"]
        n_conf += narrative_data["stage_confidence"]
        narrative_confidence = min(n_conf / 10, 1.0)

        # Overall confidence (weighted average)
        overall_confidence = archetype_confidence * 0.35
        overall_confidence += symbol_confidence * 0.25
        overall_confidence += emotion_confidence * 0.25
        overall_confidence += narrative_confidence * 0.1
        overall_confidence += historical_stability * 0.05

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
        self.confidence_threshold = (
            0.5  # Further lowered to detect more archetype shifts
        )
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

        has_primary = prev_primary and prev_primary != curr_primary
        if has_primary and curr_confidence > self.confidence_threshold:
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
                "suggested_practice": (
                    ArchetypeDefinitions.get_integration_practices().get(
                        curr_primary, "Reflective journaling and integration"
                    )
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
                "message": (
                    f"Archetype confidence {direction}: "
                    f"{confidence_delta:.2f} change"
                ),
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

        is_transform = narrative["transformation_marker"]
        is_breakthrough = narrative["hero_journey_phase"] in breakthrough_phases
        if is_transform and is_breakthrough:

            return {
                "detected": True,
                "type": "breakthrough_moment",
                "journey_phase": narrative["hero_journey_phase"],
                "is_mirror_moment": True,
                "message": (
                    "Breakthrough moment detected - " "transformation is integrating"
                ),
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
