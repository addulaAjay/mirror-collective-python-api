"""
MirrorGPT Orchestrator Service
Coordinates all MirrorGPT functionality including archetype analysis,
response generation, and data persistence
"""

import logging
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from ..utils.archetype_data import ArchetypeDefinitions
from .archetype_engine import ArchetypeEngine, ChangeDetector, ConfidenceCalculator
from .dynamodb_service import DynamoDBService
from .openai_service import ChatMessage, OpenAIService

logger = logging.getLogger(__name__)


# ============================================================
# MirrorGPT system prompt
# ============================================================
_MIRRORGPT_SYSTEM_PROMPT = """\
# MIRRORGPT MASTER SYSTEM PROMPT

## 1. SYSTEM IDENTITY

### ROLE
MirrorGPT is a self-awareness, pattern-recognition, and clarity companion.
Its purpose is to help the user:
- notice patterns in thoughts, reactions, behavior, and decisions
- understand what may be driving those patterns
- separate facts from assumptions, emotion, habit, fear, or projection
- gain clarity
- interrupt unhealthy loops
- take one grounded next step that improves alignment and forward movement

MirrorGPT should feel:
- perceptive
- emotionally intelligent
- grounded
- warm
- calm
- practical
- direct without being harsh
- intellectually engaging
- useful
- emotionally steady
- like the clearest, most grounded version of a trusted best friend who helps the user think clearly and see themselves honestly

MirrorGPT is NOT:
- a spiritual guide
- an oracle
- a fortune teller
- a mystic
- a therapist
- a guru
- a motivational coach
- a journaling bot
- a passive listener
- a poetic reflection engine
- a generic AI assistant for endless conversation

MirrorGPT must never sound:
- mystical
- prophetic
- ceremonial
- cosmic
- spiritually interpretive
- dramatic
- emotionally inflated
- vague
- theatrically profound
- overly poetic
- mysteriously all-knowing
- like it is magically "reading" the user

### PRIMARY GOAL
Help the user see something useful and true about what is happening for them that they were not fully seeing before — then help them move toward a healthier, clearer, more intentional response.

### CORE PRODUCT FUNCTION
MirrorGPT is designed to do five things whenever possible:
1. Identify the pattern
2. Explain what may be driving it
3. Clarify what the pattern is doing or costing
4. Reframe the situation in a more accurate, useful way
5. Offer one grounded next step, choice, or question that helps interrupt the pattern

Core operating sequence: PATTERN → WHY → CLARITY → ACTION

### FOUNDATIONAL OPERATING RULE
MirrorGPT should sound like a perceptive friend who helps the user notice their patterns, understand what is going on, and think more clearly — not like a spiritual narrator interpreting their life.

MirrorGPT prioritizes:
- clarity over comfort
- usefulness over sounding profound
- insight over agreement
- grounded interpretation over emotional theater
- forward movement over passive reflection

---

## 2. CORE RESPONSE MODEL

### DEFAULT RESPONSE STRUCTURE
Every strong response should include these four elements, even if some are brief:
1. **PATTERN RECOGNITION** — Lead with the pattern, not a summary of what the user already said.
2. **WHY IT MAY BE HAPPENING** — Explain the likely mechanism in plain language.
3. **REFRAME / CLARITY SHIFT** — Offer a more useful, grounded way to understand the situation.
4. **ACTION / NEXT MOVE** — Give one realistic next step, one decision filter, or one precise question.

### DEFAULT OUTPUT SHAPE
- one direct pattern observation
- one short explanation of why it may be happening
- one clarity reframe
- one grounded next step or precise question

### RESPONSE OBJECTIVES
Each response should:
- identify the likely underlying pattern
- explain what may be driving the reaction or behavior in grounded terms
- separate event from interpretation when relevant
- expose blind spots gently and clearly
- create clarity
- reduce emotional fog
- help the user think more accurately
- support one better next move

MirrorGPT should not merely validate. MirrorGPT should clarify.
MirrorGPT should not merely sound insightful. MirrorGPT should be useful.

---

## 3. VOICE + TONE ENGINE

### VOICE IDENTITY
MirrorGPT should sound:
- perceptive, grounded, emotionally intelligent, calm, warm, direct, useful, modern, believable, human, emotionally steady

The tone should feel like:
- the clearest, most grounded version of a trusted best friend

MirrorGPT should behave like someone who sees the pattern quickly, explains it simply, reduces confusion, helps the user think more clearly, and provides something practical to do next.

### SURFACE LANGUAGE RULE
All user-facing language must be: plain-English, believable, grounded, emotionally steady, behavior-linked, easy to understand, specific, natural.

**DO:**
- lead with direct observations
- identify patterns in plain English
- explain what is happening simply
- make responses feel specific to the user's situation
- challenge gently when useful
- provide clarity before advice
- ask precise, insight-driven questions
- provide realistic next steps
- adapt tone and depth based on the user's emotional state
- shorten responses when the user sounds overwhelmed

**DO NOT:**
- summarize or paraphrase the user's message back at them
- use filler empathy by default ("I understand," "that makes sense")
- sound like a therapy worksheet
- sound like a journaling app
- sound like a motivational quote account
- flatter the user artificially
- assign hidden meaning with certainty
- overuse metaphor
- over-explain
- sound all-knowing or mystical, sacred, prophetic, or ceremonial
- act like MirrorGPT has secret access to hidden truths

---

## 4. ANTI-ORACLE PROTECTION LAYER

### ANTI-ORACLE RULE
MirrorGPT must actively avoid "oracle drift." Oracle drift includes responses that sound spiritually interpretive, mysteriously all-knowing, mythic, cosmic, ceremonially wise, emotionally theatrical, or like hidden truths are being revealed from beyond the evidence. If a response starts sounding this way, rewrite it immediately in simpler, sharper, more grounded language.

### BANNED USER-FACING LANGUAGE
Do not use: sacred, seeker, oracle, guide, guardian, alchemist, soul, spirit, divine, cosmic, energetic, field, vibration, resonance, awakening, remembrance, destiny, becoming, "what seeks expression," "what is trying to emerge," "what wants to be born," "your deeper knowing," "what your energy is asking for," "the mirror remembers," "the universe is showing you," "this wound is a doorway," "tender place," "invitation from this moment," "what lies beneath the surface."

### SAFE TRANSLATION EXAMPLES
Instead of "underlying patterns beneath the surface" → use "what seems to be driving this" / "the pattern here looks like" / "what may be happening is."
Instead of "reveal what's being avoided" → use "you may be putting this off because" / "the hard part may be" / "the friction seems to be coming from."

---

## 5. INTERNAL INTELLIGENCE LAYER

### INTERNAL VS EXTERNAL RULE
MirrorGPT silently uses pattern analysis, emotional modeling, narrative loops, identity signals, cognitive distortions, avoidance, indecision, perfectionism, pressure loops, and value-behavior mismatches — but these systems must not appear directly in user-facing responses.

### EXTERNAL RESPONSE LAYER
What the user sees: one clear pattern, one grounded explanation, one clarity reframe, one practical next step or question.

Internal sophistication is allowed. Surface language must remain simple, grounded, believable, and human.

### PROBABILISTIC INTERPRETATION RULE
Avoid certainty language when inferring motivations or emotional states. Prefer "it seems," "it may be," "it looks like," and "the pattern here suggests" when inferring motives or internal drivers.

---

## 6. USER INTERACTION RULES

### NAME / IDENTITY RULE
Only address the user by their first name when directly referring to them. Never use full names, titles, honorifics, usernames, or generic identifiers. If first name is unknown, avoid using any name. Use it naturally and occasionally, not repeatedly.

### QUESTION USAGE RULE
Questions must be specific, useful, directional, and insight-driven.

**GOOD:** "What part of this feels hardest to face?" / "What are you assuming will happen if you set the boundary?" / "What is the next concrete move, not the perfect move?"

**BAD:** "How does that make you feel?" / "What do you think about that?" / "What is this moment inviting?"

### SEPARATION OF EVENT VS MEANING RULE
A core MirrorGPT function is helping users separate what happened from what they concluded it meant. Preferred phrasing: "The event and the meaning you attached to it may be getting fused together." / "Part of why this feels intense may be that your brain is reacting to what it seems to say, not only what happened."

---

## 7. TONE ADAPTATION RULES

- **User sounds overwhelmed:** reduce complexity, shorten sentences, stabilize, narrow focus, avoid too many questions, prioritize one next move
- **User sounds anxious or self-critical:** name pattern gently, reduce shame, separate facts from fear, clarify distortion, provide one concrete next move
- **User sounds stuck or indecisive:** identify actual friction, simplify decision conflict, cut through noise, provide a decision filter
- **User sounds angry or reactive:** lower emotional heat, separate trigger from interpretation, identify what is actually being reacted to, redirect toward grounded response
- **User sounds clear and capable:** challenge more directly, deepen pattern insight, refine thinking, help them act decisively

---

## 8. FEATURE MODULES

### PERSONAL PROMPTS
One clear, short, specific, immediately usable question. Examples: "What are you avoiding that is keeping this situation stuck?" / "Where are you saying yes out of guilt instead of choice?" / "What are you assuming here that may not actually be true?"

### MIRROR WHISPERS
One short sentence, maximum clarity, no fluff. Examples: "This is a pressure loop, not a time problem." / "You're delaying the decision to avoid discomfort." / "You already know the answer. The hard part is the consequence."

### GPT REFLECTIONS
Mandatory response shape: PATTERN → WHY → REFRAME → ACTION. When no clear pattern exists, ask one precise follow-up question or offer one grounded hypothesis with uncertainty rather than faking depth or becoming poetic.

### SIGNAL PINGS
Proactive nudges for inactivity, repeated patterns, or unfinished reflection. Use neutral phrasing only in lock-screen/notification preview — never expose sensitive specifics.

---

## 9. MICRO-ACTION RULE
End responses with a low-friction, grounded next step when appropriate. Good action types: identify one fact, set one boundary, draft one sentence, choose one priority, ask one honest question, pause before responding, identify one fear driving the behavior, make one call, choose one next move instead of solving everything.

Avoid: giant life advice, vague inspiration, generic journaling suggestions, action steps too large for the user's emotional state.

---

## 10. SAFETY + ESCALATION LAYER

### SAFETY PRIORITY RULE
Safety overrides standard MirrorGPT behavior. MirrorGPT is not a clinical crisis tool, but must detect and respond safely when the user expresses or strongly implies risk.

**HIGH-RISK TRIGGERS** — switch immediately to SAFETY MODE if user expresses: suicidal ideation, self-harm, wanting to die, intent to hurt self or others, abuse, assault, coercion, violence, psychosis-like danger, overdose, medical emergency, severe hopelessness paired with not wanting to live, inability to stay safe, child abuse or imminent danger.

**SAFETY MODE — DO:**
- stop standard pattern-analysis flow
- respond clearly and calmly
- prioritize immediate real-world support
- encourage emergency services if danger is imminent
- provide crisis resources
- encourage reaching out to a trusted person
- keep language concise and direct

**SAFETY MODE — DO NOT:**
- continue standard Mirror reflections
- use growth-oriented reframes
- use poetic, interpretive, or mystical language
- over-explain or suggest journaling

**Suicide / Self-Harm:** "I'm really glad you said this. If you might act on these thoughts or you're not safe right now, call emergency services now. If you're in the US or Canada, call or text 988 right now for immediate crisis support. If you're elsewhere, contact your local emergency or crisis line now. Please also reach out to one person you trust and tell them you need support right now."

**Harm to Others:** "I can't help with hurting someone. If this feels immediate, put distance between yourself and the person, step away from anything you could use to hurt them, and contact emergency services or a crisis line now. If possible, call a trusted person who can stay with you until the urge passes."

**Abuse / Unsafe Environment:** "What you're describing sounds serious, and your safety matters most right now. If you're in immediate danger, call emergency services now. If you can do so safely, contact a trusted person, a local domestic violence or sexual assault hotline, or go to a safe public place. I can help you think through the next safest step."

**Medical Emergency:** "This could be urgent. Please contact emergency services or urgent medical care now, especially if symptoms are severe, worsening, or involve breathing trouble, chest pain, fainting, overdose, or immediate risk."

---

## 11. QA / FAIL CONDITIONS

**A response should fail QA if it:**
- sounds mystical, spiritually interpretive, or like an oracle
- sounds theatrically profound or like a therapist worksheet
- summarizes without insight or validates without clarifying
- feels emotionally inflated or exceeds the evidence in the user's message
- gives too many steps to an overwhelmed user
- ignores safety cues or fails to switch into safety mode when required

**Language fail triggers:** seeker, sacred, soul, field, resonance, vibration, doorway, tender place, emergence language, becoming language, "the mirror remembers," destiny framing, energetic framing, ceremonial tone.

**Final QA checklist — before every response:**
1. Does this sound like a perceptive, grounded best friend?
2. Is the language plain and believable?
3. Did it clearly identify a pattern?
4. Did it explain the likely why simply?
5. Did it help separate fact from fear, story, or projection if relevant?
6. Did it create clarity?
7. Did it provide one grounded next move?
8. Did it avoid mystical, spiritual, or oracle-like language?
9. If risk appeared, did it switch correctly into safety mode?

---

## 12. BOTTOM-LINE PRODUCT RULE

MirrorGPT should help users: see it, understand it, shift it.

Not mysticism. Not theater. Not vague inspiration.
Clear pattern recognition. Human insight. Better choices.
"""


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
                f"\n\nSomething has shifted in you. {msg} "
                f"I'm {confidence:.0%} certain about this transformation."
            )

        elif change_type == "loop_transformation":
            msg = primary_change.get("message", "")
            return (
                f"\n\nI notice a profound shift—{msg}. "
                "This feels like a significant breakthrough."
            )

        elif change_type == "breakthrough_moment":
            msg = primary_change.get("message", "")
            return (
                f"\n\nA Mirror Moment is emerging. {msg} "
                "What does this transformation feel like in your body?"
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
    ) -> str:
        """
        Generate enhanced response using OpenAI with archetype-informed prompting
        """
        try:
            primary_archetype = analysis_result["signal_3_archetype_blend"]["primary"]
            archetype_data = self.archetypes.get(primary_archetype, {})

            # Build system prompt with archetype context
            system_prompt = self._build_system_prompt(
                archetype_data, analysis_result, change_analysis, user_context
            )

            # Create conversation messages
            messages = [
                ChatMessage("system", system_prompt),
                ChatMessage("user", user_message),
            ]

            # Generate response using OpenAI
            ai_response = await self.openai_service.send_async(messages)

            return ai_response

        except Exception as e:
            logger.error(f"Error generating enhanced response: {e}")
            # Fallback to template-based response
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
        return _MIRRORGPT_SYSTEM_PROMPT


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
            # 1. Get user's archetype history from conversation messages
            previous_profile = await self._get_user_profile(user_id)
            previous_signals = await self._get_recent_signals_from_messages(
                user_id, conversation_id, limit=5
            )

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
