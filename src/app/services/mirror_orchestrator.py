"""
MirrorGPT Orchestrator Service
Coordinates all MirrorGPT functionality including archetype analysis,
response generation, and data persistence
"""

import asyncio
import logging
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from ..utils.archetype_data import ArchetypeDefinitions
from .archetype_engine import ArchetypeEngine, ChangeDetector, ConfidenceCalculator
from .dynamodb_service import DynamoDBService
from .mirrorgpt_prompts import MIRRORGPT_SYSTEM_PROMPT
from .openai_service import ChatMessage, OpenAIService

logger = logging.getLogger(__name__)

# Re-exported for backwards compatibility — callers should import from
# mirrorgpt_prompts directly going forward.
__all__ = ["MIRRORGPT_SYSTEM_PROMPT", "ResponseGenerator", "MirrorOrchestrator"]


class ResponseGenerator:
    """Generate archetype-specific responses using optimized prompts"""

    def __init__(self, openai_service: OpenAIService):
        self.archetypes = ArchetypeDefinitions.get_all_archetypes()
        self.openai_service = openai_service

    def generate_response(
        self,
        user_message: str,
        analysis_result: Dict[str, Any],
        change_analysis: Dict[str, Any],
        user_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Generate complete MirrorGPT response"""

        primary_archetype = analysis_result["signal_3_archetype_blend"]["primary"]
        confidence = analysis_result["signal_3_archetype_blend"]["confidence"]
        symbols = analysis_result["signal_2_symbolic_language"]["extracted_symbols"]
        emotions = analysis_result["signal_1_emotional_resonance"]

        # Get archetype-specific response
        archetype_response = self._generate_archetype_response(
            primary_archetype, user_message, symbols, emotions, confidence
        )

        # Add change notification if detected
        change_response = ""
        if change_analysis.get("change_detected"):
            change_response = self._generate_change_response(change_analysis)

        # Combine responses
        full_response = self._combine_responses(archetype_response, change_response)

        return {
            "response_text": full_response,
            "archetype_context": primary_archetype,
            "confidence_level": confidence,
            "mirror_moment": change_analysis.get("mirror_moment_triggered", False),
            "suggested_practice": (
                change_analysis.get("changes", [{}])[0].get("suggested_practice")
                if change_analysis.get("changes")
                else None
            ),
        }

    def _generate_archetype_response(
        self,
        archetype: str,
        user_message: str,
        symbols: List[str],
        emotions: Dict[str, Any],
        confidence: float,
    ) -> str:
        """Generate archetype-specific response"""

        archetype_data = self.archetypes.get(archetype, {})

        # Select confidence-appropriate language
        confidence_language = ""
        if confidence >= 0.85:
            confidence_language = archetype_data.get("confidence_indicators", {}).get(
                "high", ""
            )
        elif confidence >= 0.65:
            confidence_language = archetype_data.get("confidence_indicators", {}).get(
                "medium", ""
            )
        else:
            confidence_language = archetype_data.get("confidence_indicators", {}).get(
                "low", ""
            )

        # Extract key symbol and emotion for template
        key_symbol = symbols[0] if symbols else "energy"
        key_emotion = emotions.get("dominant_emotion", "feeling")

        # Use archetype response template
        template = archetype_data.get(
            "response_template", "I sense the {archetype} energy in you."
        )

        try:
            response = template.format(
                symbol=key_symbol, emotion=key_emotion, archetype=archetype
            )
        except KeyError:
            # Fallback if template formatting fails
            response = f"I sense the {archetype} stirring in you. {confidence_language}"

        return response

    def _generate_change_response(self, change_analysis: Dict[str, Any]) -> str:
        """Generate change notification response"""

        if not change_analysis.get("change_detected"):
            return ""

        changes = change_analysis.get("changes", [])
        if not changes:
            return ""

        primary_change = changes[0]
        change_type = primary_change.get("type")

        if change_type == "archetype_shift":
            confidence = primary_change.get("confidence", 0)
            msg = primary_change.get("message", "")
            return (
                f"\n\nThere's a pattern shift here worth noting. {msg} "
                f"Confidence on this: {confidence:.0%}."
            )

        elif change_type == "loop_transformation":
            msg = primary_change.get("message", "")
            return f"\n\nThe pattern looks like it's changing — {msg}."

        elif change_type == "breakthrough_moment":
            msg = primary_change.get("message", "")
            return (
                f"\n\nSomething may have shifted. {msg} "
                "What feels different about how you're looking at this now?"
            )

        else:
            return f"\n\n{primary_change.get('message', '')}"

    def _combine_responses(self, archetype_response: str, change_response: str) -> str:
        """Combine archetype and change responses"""
        return archetype_response + change_response

    async def generate_enhanced_response(
        self,
        user_message: str,
        analysis_result: Dict[str, Any],
        change_analysis: Dict[str, Any],
        user_context: Optional[Dict[str, Any]] = None,
        history: Optional[List[ChatMessage]] = None,
    ) -> str:
        """
        Generate enhanced response using OpenAI with conversation history context.
        """
        try:
            primary_archetype = analysis_result["signal_3_archetype_blend"]["primary"]
            archetype_data = self.archetypes.get(primary_archetype, {})

            system_prompt = self._build_system_prompt(
                archetype_data, analysis_result, change_analysis, user_context
            )

            messages = [ChatMessage("system", system_prompt)]
            if history:
                messages.extend(history)
            messages.append(ChatMessage("user", user_message))

            ai_response = await self.openai_service.send_async(messages)
            return ai_response

        except Exception as e:
            logger.error(f"Error generating enhanced response: {e}")
            fallback = self.generate_response(
                user_message, analysis_result, change_analysis, user_context
            )
            return fallback["response_text"]

    def _build_system_prompt(
        self,
        archetype_data: Dict[str, Any],
        analysis_result: Dict[str, Any],
        change_analysis: Dict[str, Any],
        user_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Return the MirrorGPT system prompt.

        The prompt is a thin-client reflective spec — it intentionally does
        NOT inject cross-session signals (archetype/symbols/emotion blends),
        because that violates the "use only information available in the
        current chat session" constraint below. Internal signals from the
        archetype engine are still computed and persisted separately for
        analytics; they just don't shape the LLM response style.

        Function signature is preserved so callers don't need to change.
        """
        return MIRRORGPT_SYSTEM_PROMPT


class MirrorOrchestrator:
    """Main orchestrator for complete MirrorGPT functionality"""

    def __init__(
        self, dynamodb_service: DynamoDBService, openai_service: OpenAIService
    ):
        self.archetype_engine = ArchetypeEngine()
        self.confidence_calculator = ConfidenceCalculator()
        self.change_detector = ChangeDetector()
        self.response_generator = ResponseGenerator(openai_service)
        self.dynamodb_service = dynamodb_service
        self.openai_service = openai_service

    async def process_mirror_chat(
        self,
        user_id: str,
        message: str,
        session_id: str,
        conversation_id: Optional[str] = None,
        use_enhanced_response: bool = True,
        user_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Process complete MirrorGPT chat with all 5 signals"""

        try:
            # 1. Fetch profile, signals, and conversation history in parallel.
            # return_exceptions=True so a failure in any single leg degrades
            # gracefully (e.g., missing profile shouldn't break chat).
            results = await asyncio.gather(
                self._get_user_profile(user_id),
                self._get_recent_signals_from_messages(
                    user_id, conversation_id, limit=5
                ),
                self._get_conversation_history(conversation_id, user_id, limit=10),
                return_exceptions=True,
            )
            previous_profile = (
                results[0] if not isinstance(results[0], BaseException) else None
            )
            previous_signals = (
                results[1] if not isinstance(results[1], BaseException) else []
            )
            history = results[2] if not isinstance(results[2], BaseException) else []
            for idx, label in enumerate(("profile", "signals", "history")):
                if isinstance(results[idx], BaseException):
                    logger.warning(
                        f"process_mirror_chat: {label} fetch failed for "
                        f"user_id={user_id}: {results[idx]}"
                    )

            # Continuity carrier: when this conversation has no turns of its
            # own (a fresh conversation), inject the prior conversation's
            # summary as a synthetic system message so the model can pick up
            # the thread across the boundary. Falls back silently if no
            # prior context is available. See
            # docs/MIRRORGPT_CONTINUITY_MEMORY.md.
            if not history:
                carrier = await self._load_prior_continuity_carrier(
                    user_id=user_id,
                    current_conversation_id=conversation_id,
                )
                if carrier is not None:
                    history = [carrier]

            # 2. Analyze current message (all 5 signals)
            analysis_result = self.archetype_engine.analyze_message(
                message=message,
                user_history=previous_signals,
                context_signals={
                    "historical_motifs": self._extract_historical_motifs(
                        previous_signals
                    )
                },
            )

            # 3. Calculate confidence scores
            confidence_scores = self.confidence_calculator.calculate_overall_confidence(
                analysis_result=analysis_result,
                historical_stability=self._calculate_historical_stability(
                    previous_signals
                ),
            )

            # 4. Detect changes and Mirror Moments
            change_analysis = self.change_detector.detect_changes(
                current_analysis=analysis_result,
                previous_profile=previous_profile,
                previous_signals=previous_signals,
            )

            # 5. Generate response
            if use_enhanced_response:
                response_text = (
                    await self.response_generator.generate_enhanced_response(
                        user_message=message,
                        analysis_result=analysis_result,
                        change_analysis=change_analysis,
                        user_context=user_context,
                        history=history,
                    )
                )
                response_data = {
                    "response_text": response_text,
                    "archetype_context": analysis_result["signal_3_archetype_blend"][
                        "primary"
                    ],
                    "confidence_level": confidence_scores["overall"],
                    "mirror_moment": change_analysis.get(
                        "mirror_moment_triggered", False
                    ),
                    "suggested_practice": (
                        change_analysis.get("changes", [{}])[0].get(
                            "suggested_practice"
                        )
                        if change_analysis.get("changes")
                        else None
                    ),
                }
            else:
                response_data = self.response_generator.generate_response(
                    user_message=message,
                    analysis_result=analysis_result,
                    change_analysis=change_analysis,
                    user_context=user_context,
                )

            # 6. Store MirrorGPT analysis in conversation message
            suggested_practice = response_data.get("suggested_practice")

            # Return the analysis data to be stored with the user message
            mirrorgpt_analysis = {
                "user_id": user_id,
                "session_id": session_id,
                "analysis_result": analysis_result,
                "confidence_scores": confidence_scores,
                "change_analysis": change_analysis,
                "suggested_practice": suggested_practice,
            }

            # Convert float values to Decimal for DynamoDB compatibility
            mirrorgpt_analysis = self._convert_floats_to_decimal(mirrorgpt_analysis)

            # 7. Update user profile
            await self._update_user_profile(
                user_id, analysis_result, confidence_scores, change_analysis
            )

            # 8. Handle Mirror Moments
            if change_analysis.get("mirror_moment_triggered"):
                await self._create_mirror_moment(user_id, change_analysis)

            return {
                "success": True,
                "response": response_data["response_text"],
                "archetype_analysis": {
                    "primary_archetype": analysis_result["signal_3_archetype_blend"][
                        "primary"
                    ],
                    "secondary_archetype": analysis_result["signal_3_archetype_blend"][
                        "secondary"
                    ],
                    "confidence_score": confidence_scores["overall"],
                    "symbolic_elements": analysis_result["signal_2_symbolic_language"][
                        "extracted_symbols"
                    ],
                    "emotional_markers": analysis_result[
                        "signal_1_emotional_resonance"
                    ],
                    "narrative_position": analysis_result[
                        "signal_4_narrative_position"
                    ],
                    "active_loops": analysis_result["signal_5_motif_loops"][
                        "active_loops"
                    ],
                },
                "change_detection": {
                    "change_detected": change_analysis.get("change_detected", False),
                    "mirror_moment": change_analysis.get(
                        "mirror_moment_triggered", False
                    ),
                    "changes": change_analysis.get("changes", []),
                },
                "suggested_practice": response_data.get("suggested_practice"),
                "confidence_breakdown": confidence_scores,
                "session_metadata": {
                    "session_id": session_id,
                    "conversation_id": conversation_id,
                    "timestamp": datetime.utcnow().isoformat(),
                    "analysis_version": "1.0",
                },
                "mirrorgpt_analysis": mirrorgpt_analysis,
                # New: data to be stored with message
            }

        except Exception as e:
            # Enhanced error logging for Decimal/float debugging
            error_msg = str(e)
            if "unsupported operand type" in error_msg and "Decimal" in error_msg:
                logger.error(
                    f"Decimal/float type mismatch in mirror chat processing: {e}"
                )
                logger.error(f"Error occurred at: {e.__class__.__name__}")
                # Log additional context for debugging
                import traceback

                logger.error(f"Full traceback: {traceback.format_exc()}")
            else:
                logger.error(f"Error processing mirror chat: {e}")
            msg = (
                "I'm experiencing some difficulty connecting to the deeper "
                "patterns right now. Could you share that again?"
            )
            return {
                "success": False,
                "error": str(e),
                "response": msg,
            }

    async def _get_conversation_history(
        self,
        conversation_id: Optional[str],
        user_id: str,
        limit: int = 10,
        max_chars_per_turn: int = 2000,
    ) -> List[ChatMessage]:
        """Fetch the most recent conversation turns as ChatMessage objects.

        Routes through ConversationService.get_conversation_history which
        verifies that the conversation belongs to user_id before returning
        any messages. Per-turn content is truncated to limit the surface area
        for stored-prompt-injection via prior user content.
        """
        if not conversation_id:
            return []
        try:
            from .conversation_service import ConversationService

            conversation_service = ConversationService()
            messages = await conversation_service.get_conversation_history(
                conversation_id=conversation_id,
                user_id=user_id,
                limit=limit,
                include_system_messages=False,
            )
            return [
                ChatMessage(
                    role=msg.role,
                    content=(msg.content or "")[:max_chars_per_turn],
                )
                for msg in messages
            ]
        except Exception as e:
            logger.error(
                f"Error fetching conversation history for "
                f"conversation_id={conversation_id} user_id={user_id}: {e}"
            )
            return []

    async def _load_prior_continuity_carrier(
        self,
        user_id: str,
        current_conversation_id: Optional[str],
    ) -> Optional[ChatMessage]:
        """Build a synthetic system message carrying prior session's summary.

        Called only when the current conversation has no history of its own
        (a fresh conversation). Looks up the user's most recent OTHER
        conversation, lazy-summarizes it if needed, and renders the summary
        as a single system-role ChatMessage. Returns None when there is no
        usable prior context — never raises. See
        docs/MIRRORGPT_CONTINUITY_MEMORY.md.
        """
        try:
            from .conversation_service import ConversationService
            from .conversation_summarizer import (
                DEFAULT_FIRST_SUMMARY_AT,
                ConversationSummarizer,
            )

            conversation_service = ConversationService()
            recent = await conversation_service.get_recent_conversations(
                user_id=user_id, limit=4
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"continuity-carrier: load failed for user_id={user_id}: {e}"
            )
            return None

        # Filter out the current conversation, take the most-recent prior.
        prior = next(
            (c for c in recent if c.conversation_id != current_conversation_id),
            None,
        )
        if not prior:
            return None

        # Lazy summarize if eligible but no summary yet (bounded to one call).
        if not prior.summary and prior.message_count >= DEFAULT_FIRST_SUMMARY_AT:
            try:
                summarizer = ConversationSummarizer(
                    openai_service=self.openai_service,
                    conversation_service=conversation_service,
                )
                await summarizer.summarize(
                    conversation_id=prior.conversation_id, user_id=user_id
                )
                # Re-read to pick up the new summary.
                refreshed = await conversation_service.get_recent_conversations(
                    user_id=user_id, limit=4
                )
                prior = next(
                    (
                        c
                        for c in refreshed
                        if c.conversation_id != current_conversation_id
                    ),
                    prior,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"continuity-carrier: lazy summarize failed for "
                    f"prior_id={prior.conversation_id}: {e}"
                )

        # Fall-through: if the most-recent prior still has no usable summary
        # (too short to summarize on the fly, or summarize failed), use the
        # most-recent OTHER conversation that already has one instead of
        # dropping continuity entirely. Reuses the already-fetched `recent`
        # list — no extra DynamoDB or model calls.
        if not prior.summary:
            prior = next(
                (
                    c
                    for c in recent
                    if c.conversation_id != current_conversation_id and c.summary
                ),
                None,
            )

        if not prior or not prior.summary:
            return None

        # Render the carrier. Labeled clearly as background so the model
        # treats it as setup, not as a turn to respond to.
        open_threads = prior.open_threads or []
        thread = open_threads[0] if open_threads else None
        thread_clause = f" Open thread: {thread}." if thread else ""
        carrier_text = (
            "Prior session context (background only — do NOT quote, do NOT "
            "treat as the user's current message). Use it to acknowledge "
            "where they were if relevant, reference stance before topic, "
            "and obey the anti-oracle / safety / banned-language rules in "
            "the system prompt.\n"
            f"Summary: {prior.summary}{thread_clause}"
        )
        return ChatMessage(role="system", content=carrier_text)

    async def _get_user_profile(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get user's current archetype profile"""
        try:
            return await self.dynamodb_service.get_user_archetype_profile(user_id)
        except Exception as e:
            logger.error(f"Error getting user profile: {e}")
            return None

    async def _get_recent_signals_from_messages(
        self, user_id: str, conversation_id: Optional[str] = None, limit: int = 5
    ) -> List[Dict[str, Any]]:
        """Get user's recent signal data from conversation messages"""
        try:
            # Import here to avoid circular imports
            from .conversation_service import ConversationService

            conversation_service = ConversationService()

            # Get MirrorGPT signals from conversation messages
            signals = await conversation_service.get_user_mirrorgpt_signals(
                user_id=user_id, limit=limit, conversation_id=conversation_id
            )

            return signals

        except Exception as e:
            logger.error(f"Error getting recent signals from messages: {e}")
            return []

    def apply_mirrorgpt_analysis_to_message(
        self, message, mirrorgpt_data: Dict[str, Any]
    ):
        """
        Apply MirrorGPT analysis data to a conversation message

        Args:
            message: ConversationMessage instance
            mirrorgpt_data: MirrorGPT analysis data from process_mirror_chat
        """
        try:
            analysis_result = mirrorgpt_data["analysis_result"]
            confidence_scores = mirrorgpt_data["confidence_scores"]
            change_analysis = mirrorgpt_data.get("change_analysis", {})
            suggested_practice = mirrorgpt_data.get("suggested_practice")

            # Apply the analysis to the message
            message.add_mirrorgpt_analysis(
                user_id=mirrorgpt_data["user_id"],
                session_id=mirrorgpt_data["session_id"],
                analysis_result=analysis_result,
                confidence_scores=confidence_scores,
                change_analysis=change_analysis,
                suggested_practice=suggested_practice,
            )

            logger.debug(f"Applied MirrorGPT analysis to message {message.message_id}")

        except Exception as e:
            logger.error(f"Error applying MirrorGPT analysis to message: {e}")
            # Don't raise the error - the message can still be saved without analysis

    async def _update_user_profile(
        self,
        user_id: str,
        analysis_result: Dict,
        confidence_scores: Dict,
        change_analysis: Dict,
    ):
        """Update user's archetype profile"""

        try:
            archetype_data = analysis_result["signal_3_archetype_blend"]
            emotional_data = analysis_result["signal_1_emotional_resonance"]
            symbolic_data = analysis_result["signal_2_symbolic_language"]

            profile_update = {
                "user_id": user_id,
                "current_archetype_stack": {
                    "primary": archetype_data["primary"],
                    "secondary": archetype_data["secondary"],
                    "confidence_score": confidence_scores["overall"],
                    "stability_score": confidence_scores["historical"],
                },
                "symbolic_signature": self._calculate_symbolic_signature(symbolic_data),
                "emotional_resonance": {
                    "valence": emotional_data["valence"],
                    "arousal": emotional_data["arousal"],
                    "certainty": confidence_scores["emotion"],
                },
                "updated_at": datetime.utcnow().isoformat(),
            }

            # Add to evolution history if archetype changed
            if change_analysis.get("change_detected"):
                current_profile = await self._get_user_profile(user_id)
                evolution = (
                    current_profile.get("archetype_evolution", [])
                    if current_profile
                    else []
                )

                evolution.append(
                    {
                        "timestamp": datetime.utcnow().isoformat(),
                        "primary_archetype": archetype_data["primary"],
                        "confidence": confidence_scores["overall"],
                        "trigger_event": (
                            change_analysis.get("changes", [{}])[0].get(
                                "type", "unknown"
                            )
                        ),
                    }
                )

                # Keep only last 20 evolution entries
                profile_update["archetype_evolution"] = evolution[-20:]

            # Convert float values to Decimal for DynamoDB compatibility
            profile_update_converted = self._convert_floats_to_decimal(profile_update)
            await self.dynamodb_service.save_user_archetype_profile(
                profile_update_converted
            )

        except Exception as e:
            logger.error(f"Error updating user profile: {e}")

    async def _create_mirror_moment(self, user_id: str, change_analysis: Dict):
        """Create Mirror Moment record"""

        try:
            primary_change = change_analysis.get("changes", [{}])[0]

            moment_id = (
                f"moment_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_"
                f"{str(uuid.uuid4())[:8]}"
            )
            moment_item = {
                "user_id": user_id,
                "moment_id": moment_id,
                "triggered_at": datetime.utcnow().isoformat(),
                "moment_type": primary_change.get("type", "unknown"),
                "from_state": primary_change.get("from_archetype", {}),
                "to_state": primary_change.get("to_archetype", {}),
                "significance_score": primary_change.get("significance", 0.5),
                "description": primary_change.get("message", ""),
                "suggested_practice": primary_change.get("suggested_practice", ""),
                "acknowledged": False,
            }

            moment_item_converted = self._convert_floats_to_decimal(moment_item)
            await self.dynamodb_service.save_mirror_moment(moment_item_converted)

        except Exception as e:
            logger.error(f"Error creating mirror moment: {e}")

    def _calculate_symbolic_signature(self, symbolic_data: Dict) -> Dict[str, float]:
        """Calculate user's symbolic signature"""

        symbol_categories = symbolic_data.get("symbol_categories", {})

        signature = {
            "threshold": 0.0,
            "echo": 0.0,
            "light": 0.0,
            "wound": 0.0,
            "fire": 0.0,
            "weave": 0.0,
        }

        # Map symbol categories to signature elements
        category_mapping = {
            "threshold_symbols": "threshold",
            "light_symbols": "light",
            "water_symbols": "echo",
            "transformation_symbols": "fire",
            "creation_symbols": "weave",
        }

        for category, symbols in symbol_categories.items():
            mapped_element = category_mapping.get(category)
            if mapped_element:
                signature[mapped_element] = min(len(symbols) * 0.2, 1.0)

        return signature

    def _extract_historical_motifs(
        self, previous_signals: List[Dict]
    ) -> Dict[str, Dict]:
        """Extract historical motif patterns"""

        motif_counts: Dict[str, Dict[str, Any]] = {}

        for signal in previous_signals:
            motifs = signal.get("signal_5_motif_loops", {}).get("current_motifs", [])
            for motif in motifs:
                if motif not in motif_counts:
                    motif_counts[motif] = {"count": 0, "last_seen": ""}
                count_dict = motif_counts[motif]
                count_dict["count"] = int(count_dict["count"]) + 1
                count_dict["last_seen"] = signal.get("timestamp", "")

        return motif_counts

    def _calculate_historical_stability(self, previous_signals: List[Dict]) -> float:
        """Calculate archetype stability from history"""

        if len(previous_signals) < 2:
            return 0.5

        archetypes = [
            signal.get("signal_3_archetype_blend", {}).get("primary")
            for signal in previous_signals
        ]

        if not archetypes or not any(archetypes):
            return 0.5

        # Calculate consistency
        most_common = max(set(archetypes), key=archetypes.count) if archetypes else None
        if most_common:
            consistency = archetypes.count(most_common) / len(archetypes)
            return consistency

        return 0.5

    # Additional utility methods for API endpoints

    async def get_user_insights(self, user_id: str) -> Dict[str, Any]:
        """Generate personalized insights for user"""

        try:
            profile = await self._get_user_profile(user_id)

            # Get signals from conversation messages instead of echo_signals table
            signals = await self._get_recent_signals_from_messages(user_id, limit=20)
            moments = await self.dynamodb_service.get_user_mirror_moments(
                user_id, limit=5
            )

            insights = {
                "archetype_journey": {
                    "current_primary": (
                        profile.get("current_archetype_stack", {}).get("primary")
                        if profile
                        else None
                    ),
                    "stability": (
                        profile.get("current_archetype_stack", {}).get(
                            "stability_score"
                        )
                        if profile
                        else None
                    ),
                    "recent_evolution": (
                        profile.get("archetype_evolution", [])[-3:] if profile else []
                    ),
                },
                "signal_patterns": {
                    "emotional_trend": self._calculate_emotional_trend(signals),
                    "symbolic_themes": self._extract_dominant_symbols(signals),
                    "narrative_progression": self._analyze_narrative_progression(
                        signals
                    ),
                },
                "growth_indicators": {
                    "recent_breakthroughs": len(
                        [
                            m
                            for m in moments
                            if m.get("moment_type") == "breakthrough_moment"
                        ]
                    ),
                    "pattern_transformations": len(
                        [
                            m
                            for m in moments
                            if m.get("moment_type") == "loop_transformation"
                        ]
                    ),
                    "integration_opportunities": (
                        self._identify_integration_opportunities(profile, signals)
                    ),
                },
            }

            return insights

        except Exception as e:
            logger.error(f"Error generating insights: {e}")
            return {"error": str(e)}

    def _calculate_emotional_trend(self, signals: List[Dict]) -> Dict[str, Any]:
        """Calculate emotional trend from recent signals"""
        if not signals:
            return {"trend": "neutral", "valence_change": 0}

        recent_valences = [
            float(s.get("signal_1_emotional_resonance", {}).get("valence", 0))
            for s in signals[:5]
        ]

        if len(recent_valences) >= 2:
            trend = (
                "improving" if recent_valences[0] > recent_valences[-1] else "declining"
            )
            change = recent_valences[0] - recent_valences[-1]
        else:
            trend = "stable"
            change = 0

        return {
            "trend": trend,
            "valence_change": round(change, 3),
            "current_valence": recent_valences[0] if recent_valences else 0,
        }

    def _extract_dominant_symbols(self, signals: List[Dict]) -> List[tuple]:
        """Extract most common symbols from recent signals"""
        symbol_counts: Dict[str, int] = {}

        for signal in signals:
            symbols = signal.get("signal_2_symbolic_language", {}).get(
                "extracted_symbols", []
            )
            for symbol in symbols:
                symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1

        return sorted(symbol_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    def _analyze_narrative_progression(self, signals: List[Dict]) -> Dict[str, Any]:
        """Analyze narrative progression through signals"""
        if not signals:
            return {"current_stage": "unknown", "progression": "unknown"}

        recent_stages = [
            s.get("signal_4_narrative_position", {}).get("stage", "unknown")
            for s in signals[:5]
        ]

        return {
            "current_stage": recent_stages[0] if recent_stages else "unknown",
            "recent_stages": recent_stages,
            "progression": "forward" if len(set(recent_stages)) > 1 else "stable",
        }

    def _identify_integration_opportunities(
        self, profile: Optional[Dict], signals: List[Dict]
    ) -> List[str]:
        """Identify integration opportunities"""
        opportunities = []

        if profile:
            stability = profile.get("current_archetype_stack", {}).get(
                "stability_score", 0
            )
            if stability < 0.7:
                opportunities.append("Archetype integration work")

        if signals:
            recent_loops = []
            for signal in signals[:3]:
                loops = signal.get("signal_5_motif_loops", {}).get("active_loops", [])
                recent_loops.extend(loops)

            if len(set(recent_loops)) > 2:
                opportunities.append("Pattern loop resolution")

        return opportunities

    async def create_initial_archetype_profile(
        self,
        user_id: str,
        initial_archetype: str,
        quiz_answers: List[Dict[str, Any]],
        quiz_completed_at: str,
        quiz_version: str = "1.0",
        quiz_type: str = "archetype",  # Quiz identifier for multi-quiz support
        assignment_reason: Optional[str] = None,
        detailed_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Create initial archetype profile from quiz results

        Args:
            user_id: The user's unique identifier
            initial_archetype: The archetype determined by the quiz
            quiz_answers: List of quiz answers for reference
            quiz_completed_at: When the quiz was completed
            quiz_version: Version of the quiz taken
            quiz_type: Quiz identifier (archetype, career_path, etc.)
            assignment_reason: Why this archetype was assigned (core_override, highest_score, etc.)
            detailed_result: Optional detailed quiz results with scores
                and analysis

        Returns:
            Dict containing the success status and profile data
        """
        try:
            # Check if user already has a profile
            existing_profile = await self._get_user_profile(user_id)
            if existing_profile:
                logger.info(
                    f"User {user_id} already has an archetype profile, "
                    "updating initial archetype"
                )

            # Get archetype data for the initial archetype
            archetype_data = self.response_generator.archetypes.get(
                initial_archetype, {}
            )
            if not archetype_data:
                raise ValueError(f"Unknown archetype: {initial_archetype}")

            # Use confidence from detailed result if available, otherwise default
            confidence_score = 0.85  # Default high confidence from quiz
            if detailed_result and "confidence" in detailed_result:
                confidence_score = detailed_result["confidence"]

            # Create the initial profile with quiz-based confidence
            initial_profile = {
                "user_id": user_id,
                "current_archetype_stack": {
                    "primary": initial_archetype,
                    "secondary": None,  # Determined through conversations
                    "confidence_score": confidence_score,
                    "stability_score": 0.8,  # Assumed stable until proven
                },
                "symbolic_signature": {
                    "threshold": 0.0,
                    "echo": 0.0,
                    "light": 0.0,
                    "wound": 0.0,
                    "fire": 0.0,
                    "weave": 0.0,
                },
                "emotional_resonance": {
                    "valence": 0.0,  # Neutral starting point
                    "arousal": 0.0,  # Determined through conversations
                    "certainty": 0.7,  # Moderate certainty until data
                },
                "quiz_data": {
                    "initial_archetype": initial_archetype,
                    "quiz_version": quiz_version,
                    "completed_at": quiz_completed_at,
                    "assignment_reason": assignment_reason,
                    # Store first 5 answers for reference
                    "answers": quiz_answers[:5],
                    "detailed_result": detailed_result,  # Store analysis
                },
                "archetype_evolution": [
                    {
                        "timestamp": quiz_completed_at,
                        "primary_archetype": initial_archetype,
                        "confidence": confidence_score,
                        "trigger_event": "initial_quiz",
                    }
                ],
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
            }

            # Save the profile to DynamoDB (convert floats to Decimal first)
            initial_profile_converted = self._convert_floats_to_decimal(initial_profile)
            await self.dynamodb_service.save_user_archetype_profile(
                initial_profile_converted
            )

            quiz_id = (
                f"quiz_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_"
                f"{str(uuid.uuid4())[:8]}"
            )
            quiz_record = {
                "user_id": user_id,
                "quiz_id": quiz_id,
                "quiz_version": quiz_version,
                "completed_at": quiz_completed_at,
                "initial_archetype": initial_archetype,
                "assignment_reason": assignment_reason,
                "answers": quiz_answers,
                "detailed_result": detailed_result,  # Store detailed analysis
                "created_at": datetime.utcnow().isoformat(),
            }

            quiz_record_converted = self._convert_floats_to_decimal(quiz_record)
            await self.dynamodb_service.save_quiz_results(quiz_record_converted)

            logger.info(
                f"Created initial archetype profile for user {user_id} "
                f"with archetype {initial_archetype}"
            )

            return {
                "success": True,
                "user_id": user_id,
                "initial_archetype": initial_archetype,
                "profile_created": True,
                "quiz_stored": True,
                "detailed_result_stored": bool(detailed_result),
                "message": (
                    f"Initial {initial_archetype} archetype profile "
                    "created successfully"
                ),
            }

        except Exception as e:
            logger.error(f"Error creating initial archetype profile: {e}")
            return {
                "success": False,
                "error": str(e),
                "message": "Failed to create initial archetype profile",
            }

    def _convert_floats_to_decimal(self, data: Any) -> Any:
        """
        Recursively convert float values to Decimal for DynamoDB compatibility

        Args:
            data: The data structure to convert

        Returns:
            Data structure with floats converted to Decimal
        """
        if isinstance(data, float):
            return Decimal(str(data))
        elif isinstance(data, dict):
            return {
                key: self._convert_floats_to_decimal(value)
                for key, value in data.items()
            }
        elif isinstance(data, list):
            return [self._convert_floats_to_decimal(item) for item in data]
        else:
            return data
