"""
MirrorGPT API Routes
Extends existing API structure with MirrorGPT-specific endpoints
"""

import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Dict, List, Optional

# Strict allow-list for names that get interpolated into LLM prompt text.
# Keeps letters, spaces, apostrophes, hyphens, periods. Caps at 50 chars.
# Defends against prompt-injection via user-controlled display name
# (e.g. names crafted to break out of the trigger sentence).
_NAME_SAFE_CHARS = re.compile(r"[^A-Za-z '\-.]")


def _sanitize_name(name: Optional[str]) -> str:
    if not name:
        return ""
    cleaned = _NAME_SAFE_CHARS.sub("", name).strip()
    return cleaned[:50]


from fastapi import APIRouter, Depends, HTTPException, Query

if TYPE_CHECKING:
    from ..services.conversation_service import ConversationService

from ..core.enhanced_auth import get_user_with_profile
from ..core.security import get_current_user, get_current_user_optional
from ..services.dynamodb_service import (  # noqa: F401  (DynamoDBService patched in tests/conftest)
    DynamoDBService,
    get_dynamodb_service,
)
from ..services.mirror_orchestrator import MIRRORGPT_SYSTEM_PROMPT, MirrorOrchestrator
from ..services.openai_service import (  # noqa: F401  (OpenAIService patched in tests/conftest)
    ChatMessage,
    OpenAIService,
    get_openai_service,
)
from ..services.quiz_questions_loader import (
    get_quiz_questions as load_bundled_quiz_questions,
)
from ..services.quiz_questions_loader import load_quiz_data
from .models import (
    ArchetypeAnalysisData,
    ArchetypeAnalysisRequest,
    ArchetypeAnalysisResponse,
    ArchetypeProfileData,
    ArchetypeProfileResponse,
    ArchetypeQuizData,
    ArchetypeQuizRequest,
    ArchetypeQuizResponse,
    EchoSignalData,
    EchoSignalResponse,
    MirrorGPTChatData,
    MirrorGPTChatRequest,
    MirrorGPTChatResponse,
    MirrorMomentAcknowledgeResponse,
    MirrorMomentData,
    MirrorMomentResponse,
    PatternLoopData,
    PatternLoopResponse,
    UserInsightsData,
    UserInsightsResponse,
)

logger = logging.getLogger(__name__)


def generate_conversation_title(message: str, max_length: int = 50) -> str:
    """
    Generate a dynamic conversation title from the user's first message

    Args:
        message: The user's first message
        max_length: Maximum length for the title

    Returns:
        A meaningful title based on the message content

    # Examples:
    # "I'm feeling lost at a crossroads"
    #   → "I'm feeling lost at a crossroads"
    # "I keep having dreams about water and transformation"
    #   → "Dreams about water and transformation"
    # "There's this calling in my soul but I don't know what it means"
    #   → "This calling in my soul..."
    """
    import re

    # Clean the message: remove extra whitespace and newlines
    clean_message = re.sub(r"\s+", " ", message.strip())

    # If message is short enough, use it directly
    if len(clean_message) <= max_length:
        return clean_message

    # Try to find meaningful keywords and phrases
    keywords = []

    # Look for emotional/symbolic keywords that are meaningful in MirrorGPT context
    meaningful_patterns = [
        r"\b(feeling|feel|emotions?|heart|soul|spirit)\b",
        r"\b(journey|path|crossroads?|threshold|transition)\b",
        r"\b(transformation?|change|growth|evolution)\b",
        r"\b(seeking|searching?|looking|wondering)\b",
        r"\b(dreams?|visions?|symbols?|signs?)\b",
        r"\b(calling|purpose|meaning|direction)\b",
        r"\b(breakthrough|awakening|realization)\b",
        r"\b(mirror|reflection|pattern|loop)\b",
        r"\b(light|shadow|darkness|healing)\b",
        r"\b(wisdom|guidance|truth|clarity)\b",
    ]

    # Extract meaningful phrases
    for pattern in meaningful_patterns:
        matches = re.findall(pattern, clean_message.lower())
        keywords.extend(matches)

    # If we found meaningful keywords, try to create a title with context
    if keywords:
        # Try to get the sentence containing the first meaningful keyword
        first_keyword = keywords[0]
        sentences = re.split(r"[.!?]+", clean_message)

        for sentence in sentences:
            sentence = sentence.strip()
            if first_keyword in sentence.lower():
                if len(sentence) <= max_length:
                    return sentence

    # Fallback: Take first sentence or truncate intelligently
    sentences = re.split(r"[.!?]+", clean_message)
    first_sentence = sentences[0].strip()

    if len(first_sentence) <= max_length:
        return first_sentence

    # Truncate at word boundary
    truncated = clean_message[:max_length]
    last_space = truncated.rfind(" ")

    if last_space > max_length * 0.7:  # Don't truncate too aggressively
        return truncated[:last_space] + "..."
    else:
        return truncated + "..."


# Create router for MirrorGPT endpoints
router = APIRouter(prefix="/mirrorgpt", tags=["MirrorGPT"])


@lru_cache(maxsize=1)
def get_mirror_orchestrator() -> MirrorOrchestrator:
    """Dependency injection for MirrorOrchestrator.

    Cached process-wide: the orchestrator is stateless (user/session data is
    passed per call), and its deps — DynamoDBService, OpenAIService — are
    themselves cached singletons. Caching avoids rebuilding the OpenAI httpx
    clients and re-loading ArchetypeEngine definitions on every request.
    """
    return MirrorOrchestrator(get_dynamodb_service(), get_openai_service())


def get_conversation_service() -> "ConversationService":
    """Dependency injection for ConversationService"""
    from ..services.conversation_service import ConversationService

    return ConversationService()


def _schedule_summary_refresh(
    conversation_service: "ConversationService",
    conversation_id: str,
    user_id: str,
) -> None:
    """Fire-and-forget continuity summary refresh.

    Runs after we've saved both the user message and the assistant
    response. Threshold/staleness logic lives inside
    ConversationSummarizer; this just kicks the task. Errors are
    swallowed — continuity is a best-effort enrichment, never a chat
    blocker.
    """

    async def _run() -> None:
        try:
            from ..services.conversation_summarizer import ConversationSummarizer

            summarizer = ConversationSummarizer(
                openai_service=get_openai_service(),
                conversation_service=conversation_service,
            )
            # summarize_if_stale handles "do we need to" internally.
            await summarizer.summarize_if_stale(
                conversation_id=conversation_id, user_id=user_id
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "summary refresh task failed for "
                f"conversation_id={conversation_id}: {e}"
            )

    try:
        asyncio.create_task(_run())
    except RuntimeError:
        # No running event loop (sync test contexts, etc.) — skip silently.
        logger.debug("summary refresh: no running event loop; skipping")


# Bounded number of prior summaries we surface in the greeting prompt.
_MAX_GREETING_CONTEXT = int(os.getenv("MIRRORGPT_SUMMARY_MAX_GREETING_CONTEXT", "3"))


def _format_age_label(timestamp: Optional[str], now: Optional[datetime] = None) -> str:
    """Render a soft relative age like '2 days ago' or 'earlier today'.

    Used in continuity context so the greeting can land naturally without
    surfacing raw ISO timestamps. Falls back to 'recently' on parse error.
    """
    if not timestamp:
        return "recently"
    try:
        # Project convention: ISO with trailing Z.
        normalized = timestamp.replace("Z", "+00:00")
        ts = datetime.fromisoformat(normalized)
    except (ValueError, AttributeError):
        return "recently"

    reference = now or datetime.now(timezone.utc)
    delta = reference - ts
    minutes = int(delta.total_seconds() // 60)

    if minutes < 0:
        return "just now"
    if minutes < 15:
        return "just now"
    if minutes < 60 * 6:
        return "earlier today"
    if minutes < 60 * 24:
        return "today"
    days = minutes // (60 * 24)
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    weeks = days // 7
    if weeks == 1:
        return "last week"
    if weeks < 4:
        return f"{weeks} weeks ago"
    return "a while back"


async def _load_continuity_context(
    user_id: str,
    conversation_service: "ConversationService",
    limit: int = _MAX_GREETING_CONTEXT,
) -> Dict[str, Any]:
    """Load up to `limit` recent conversations for greeting continuity.

    Returns:
        {
          "resume_conversation_id": Optional[str],  # most recent, even if unsummarized
          "context_lines": List[str],               # ready-to-render bullet lines
          "has_prior_context": bool,                # at least one usable summary
        }

    Triggers lazy-on-read summarization on the single most-recent
    conversation if it has enough messages but no summary yet. Latency is
    bounded to one summarizer call.
    """
    empty: Dict[str, Any] = {
        "resume_conversation_id": None,
        "context_lines": [],
        "has_prior_context": False,
    }

    try:
        recent = await conversation_service.get_recent_conversations(
            user_id=user_id, limit=limit
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"continuity: get_recent_conversations failed: {e}")
        return empty

    if not recent:
        return empty

    # Lazy-on-read: refresh the single most-recent conversation's summary if
    # it's missing OR stale (summarize_if_stale decides internally). We only
    # do this for the top one to cap greeting latency. This is the reliable
    # refresh path — the post-chat fire-and-forget task is not guaranteed to
    # complete on Lambda before the environment freezes, so without this the
    # greeting can recap a summary that's several sessions out of date.
    most_recent = recent[0]
    summary_result = await _try_lazy_summarize(
        conversation_service=conversation_service,
        conversation=most_recent,
        user_id=user_id,
    )

    # Apply a freshly-generated (or refreshed) summary in place rather than
    # re-querying all recent conversations from DynamoDB. Lazy summarize only
    # ever touches `most_recent`, which is `recent[0]` — the same object we
    # render below — so mutating it is equivalent to the old re-read but saves
    # one DynamoDB round-trip on every greeting.
    if summary_result is not None:
        most_recent.summary = summary_result.summary
        most_recent.key_themes = summary_result.key_themes
        most_recent.open_threads = summary_result.open_threads

    context_lines: List[str] = []
    for conv in recent:
        if not conv.summary:
            continue
        age = _format_age_label(conv.last_message_at)
        # Tag the latest summarized conversation explicitly so the greeting
        # LLM anchors on it instead of picking an older line at random.
        recency_tag = "most recent — " if not context_lines else ""
        line = f"- ({recency_tag}{age}) {conv.summary}"
        threads = (conv.open_threads or [])[:1]
        if threads:
            line += f" Open thread: {threads[0]}."
        context_lines.append(line)

    return {
        "resume_conversation_id": most_recent.conversation_id,
        "context_lines": context_lines,
        "has_prior_context": bool(context_lines),
    }


async def _try_lazy_summarize(
    conversation_service: "ConversationService",
    conversation,
    user_id: str,
) -> Optional[Any]:
    """Best-effort lazy summarization for the most-recent conversation.

    Cheap pre-check on the first-summary threshold, then delegates the
    missing-vs-stale-vs-fresh decision to summarize_if_stale so a stale
    summary gets refreshed too. Errors are swallowed.

    Returns the resulting SummaryResult (freshly generated or the existing
    one when still fresh) so the caller can apply it without re-reading from
    DynamoDB; returns None when nothing was produced (below threshold or
    error).
    """
    try:
        from ..services.conversation_summarizer import (
            DEFAULT_FIRST_SUMMARY_AT,
            ConversationSummarizer,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"continuity: summarizer import failed: {e}")
        return None

    if conversation.message_count < DEFAULT_FIRST_SUMMARY_AT:
        return None

    try:
        summarizer = ConversationSummarizer(
            openai_service=get_openai_service(),
            conversation_service=conversation_service,
        )
        return await summarizer.summarize_if_stale(
            conversation_id=conversation.conversation_id,
            user_id=user_id,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"continuity: lazy summarize failed for "
            f"conversation_id={conversation.conversation_id}: {e}"
        )
        return None


@router.post("/chat", response_model=MirrorGPTChatResponse)
async def mirrorgpt_chat(
    request: MirrorGPTChatRequest,
    conversation_service: "ConversationService" = Depends(get_conversation_service),
    current_user: Dict[str, Any] = Depends(get_user_with_profile),
    orchestrator: MirrorOrchestrator = Depends(get_mirror_orchestrator),
):
    """
    Enhanced mirror chat with complete archetype analysis and pattern detection

    This endpoint provides the core MirrorGPT functionality including:
    - 5-signal archetypal analysis
    - Mirror Moment detection
    - Symbolic language processing
    - Pattern loop identification
    - Personalized responses based on archetype
    """

    try:
        session_id = request.session_id or str(uuid.uuid4())
        conversation_id = request.conversation_id

        # Create or get existing conversation
        if not conversation_id:
            # Generate dynamic title from user's message
            conversation_title = generate_conversation_title(request.message)

            # Create new conversation for MirrorGPT chat
            conversation_result = await conversation_service.create_conversation(
                user_id=current_user["id"], title=conversation_title
            )
            if conversation_result and hasattr(conversation_result, "conversation_id"):
                conversation_id = conversation_result.conversation_id
            else:
                logger.warning(
                    "Failed to create conversation, proceeding without conversation_id"
                )

        # Extract user context for personalized response
        user_context = {
            "id": current_user["id"],
            "name": current_user.get("name") or "",
            "email": current_user.get("email"),
        }

        result = await orchestrator.process_mirror_chat(
            user_id=current_user["id"],
            message=request.message,
            session_id=session_id,
            conversation_id=conversation_id,
            use_enhanced_response=request.use_enhanced_response,
            user_context=user_context,
        )

        if not result.get("success"):
            logger.error(
                "process_mirror_chat returned failure for user_id=%s: %s",
                current_user["id"],
                result.get("error"),
            )
            raise HTTPException(status_code=500, detail="Chat processing failed")

        # Save the user message and AI response to conversation
        if conversation_id:
            try:
                # The user-message and assistant-message writes are independent
                # DDB items — persist them concurrently (one round-trip instead
                # of two). Both must complete before we return: on Lambda the
                # container freezes after the response, so a fire-and-forget
                # write would be lost.
                await asyncio.gather(
                    conversation_service.add_message_with_mirrorgpt_analysis(
                        conversation_id=conversation_id,
                        user_id=current_user["id"],
                        role="user",
                        content=request.message,
                        mirrorgpt_analysis=result.get("mirrorgpt_analysis"),
                    ),
                    conversation_service.add_message(
                        conversation_id=conversation_id,
                        user_id=current_user["id"],
                        role="assistant",
                        content=result["response"],
                    ),
                )

                logger.debug(f"Saved messages to conversation {conversation_id}")

                # Fire-and-forget continuity summary refresh.
                # See docs/MIRRORGPT_CONTINUITY_MEMORY.md — threshold logic
                # lives inside maybe_summarize_after_chat() so the route
                # stays unaware of the policy.
                _schedule_summary_refresh(
                    conversation_service=conversation_service,
                    conversation_id=conversation_id,
                    user_id=current_user["id"],
                )
            except Exception as e:
                logger.warning(f"Failed to save messages to conversation: {e}")

        # Format response data
        chat_data = MirrorGPTChatData(
            message_id=str(uuid.uuid4()),
            response=result["response"],
            archetype_analysis=result["archetype_analysis"],
            change_detection=result["change_detection"],
            suggested_practice=result.get("suggested_practice"),
            confidence_breakdown=result["confidence_breakdown"],
            session_metadata={
                **result["session_metadata"],
                # Ensure conversation_id is included
                "conversation_id": conversation_id,
            },
        )

        return MirrorGPTChatResponse(success=True, data=chat_data)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            "Mirror chat failed for user_id=%s: %s", current_user.get("id"), e
        )
        raise HTTPException(status_code=500, detail="Mirror chat failed")


@router.post("/analyze", response_model=ArchetypeAnalysisResponse)
async def analyze_archetype(
    request: ArchetypeAnalysisRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    orchestrator: MirrorOrchestrator = Depends(get_mirror_orchestrator),
):
    """
    Standalone archetype analysis without response generation

    Provides detailed archetype detection and pattern analysis
    for a given message without generating a conversational response.
    Useful for analysis tools and dashboards.
    """

    try:
        analysis = orchestrator.archetype_engine.analyze_message(
            message=request.message,
            user_history=None,  # No history context for standalone analysis
            context_signals=None,
        )

        confidence = orchestrator.confidence_calculator.calculate_overall_confidence(
            analysis
        )

        # Get archetype description
        archetype_name = analysis["signal_3_archetype_blend"]["primary"]
        archetype_data = orchestrator.response_generator.archetypes.get(
            archetype_name, {}
        )

        analysis_data = ArchetypeAnalysisData(
            primary_archetype=archetype_name,
            secondary_archetype=analysis["signal_3_archetype_blend"]["secondary"],
            confidence_score=confidence["overall"],
            symbolic_elements=analysis["signal_2_symbolic_language"][
                "extracted_symbols"
            ],
            emotional_markers=analysis["signal_1_emotional_resonance"],
            narrative_position=analysis["signal_4_narrative_position"],
            active_motifs=analysis["signal_5_motif_loops"]["current_motifs"],
            archetype_description=archetype_data.get("core_resonance", ""),
        )

        return ArchetypeAnalysisResponse(success=True, data=analysis_data)

    except Exception as e:
        logger.error(f"Archetype analysis failed: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Archetype analysis failed: {str(e)}"
        )


@router.get("/quiz/questions")
async def get_quiz_questions():
    """
    Get all active quiz questions.

    Served from the bundled questions.json (static V1 content) instead of a
    DynamoDB scan, so this is a fast, cold-start-friendly read. No orchestrator
    dependency — avoids constructing an OpenAIService per request. Keep the
    file in sync with the table via scripts/export_quiz_questions.py.
    """
    try:
        return {"success": True, "data": load_bundled_quiz_questions()}
    except Exception as e:
        logger.error(f"Failed to get quiz questions: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Failed to get quiz questions: {str(e)}"
        )


@router.post("/quiz/submit", response_model=ArchetypeQuizResponse)
async def submit_archetype_quiz(
    request: ArchetypeQuizRequest,
    current_user: Optional[Dict[str, Any]] = Depends(get_current_user_optional),
    orchestrator: MirrorOrchestrator = Depends(get_mirror_orchestrator),
):
    """
    Submit archetype quiz and calculate results server-side (V1 Spec)

    This endpoint:
    1. Receives raw quiz answers from frontend
    2. Calculates archetype using weighted scoring logic
    3. Returns calculated result with assignment reason
    4. Stores profile with calculated archetype

    Supports both authenticated users and anonymous submissions.
    """
    from ..services.quiz_scoring import calculate_quiz_result
    from ..services.quiz_submission import build_scoring_inputs

    try:
        # 1. Determine user ID
        user_id = None
        if current_user:
            user_id = current_user["id"]
        elif request.anonymousId:
            user_id = f"anon_{request.anonymousId}"
        else:
            raise HTTPException(
                status_code=401,
                detail="Authentication required or anonymousId must be provided",
            )

        # 2. Load questions + config from the bundled questions.json (static V1
        #    content baked into the deploy — no DynamoDB scan on the request
        #    path). Keep it in sync with the table via the export script.
        questions_json = load_quiz_data()
        questions_list = questions_json.get("questions", [])

        if not questions_list:
            raise HTTPException(status_code=500, detail="No quiz questions found")

        # 3. Extract quiz config for dynamic scoring
        quiz_config = questions_json.get("config", {})

        # Build list of core question IDs from question data (dynamic).
        # Coerce to int (DynamoDB returns numbers as Decimal).
        core_question_ids = [
            int(q["id"])
            for q in questions_list
            if q.get("core", False) and q.get("id") is not None
        ]
        if not core_question_ids:
            # Fallback to default if no core questions specified
            core_question_ids = [1, 3, 5]

        # Add core questions to config for scoring engine
        if quiz_config and "coreQuestions" not in quiz_config:
            quiz_config["coreQuestions"] = core_question_ids

        # 4. Map + validate answers (dedupe, completeness, robust extraction).
        #    Core flag follows the configured core questions, not a hardcoded set.
        scoring_answers, quiz_answers_for_storage = build_scoring_inputs(
            request.answers, questions_list, core_question_ids
        )

        # 5. Calculate quiz result using the quiz's own config (weights, core
        #    questions, tie-break order) rather than the hardcoded defaults.
        quiz_result = calculate_quiz_result(scoring_answers, quiz_config)

        # 7. Create initial archetype profile with calculated result
        result = await orchestrator.create_initial_archetype_profile(
            user_id=user_id,
            initial_archetype=quiz_result["final_archetype"],
            quiz_answers=quiz_answers_for_storage,
            quiz_completed_at=request.completedAt,
            quiz_version=request.quizVersion,
            quiz_type=request.quiz_type,  # Store quiz type identifier
            assignment_reason=quiz_result["assignment_reason"],
            detailed_result={
                "scores": quiz_result["total_scores"],
                "primary_archetype": quiz_result["final_archetype"],
                "confidence": 0.85,  # High confidence from quiz
                "analysis": {
                    "strengths": [],
                    "challenges": [],
                    "recommendations": [],
                },
            },
        )

        if not result.get("success"):
            raise HTTPException(
                status_code=500,
                detail=result.get("error", "Failed to create archetype profile"),
            )

        # 8. Load archetype metadata for response
        archetype_key = quiz_result["final_archetype"].lower()
        archetype_metadata = questions_json.get("archetypes", {}).get(archetype_key)

        if not archetype_metadata:
            raise HTTPException(
                status_code=500,
                detail=f"Archetype metadata not found for {quiz_result['final_archetype']}",
            )

        # 9. Format response with calculated result and metadata
        quiz_data = ArchetypeQuizData(
            quiz_type=request.quiz_type,  # Include quiz type in response
            user_id=user_id,
            final_archetype=quiz_result["final_archetype"],
            assignment_reason=quiz_result["assignment_reason"],
            total_scores=quiz_result["total_scores"],
            archetype_details=archetype_metadata,
            quiz_completed_at=request.completedAt,
            quiz_version=request.quizVersion,
            profile_created=result.get("profile_created", True),
            answers_stored=result.get("quiz_stored", True),
        )

        msg = (
            f"Initial {quiz_result['final_archetype']} archetype profile "
            f"created successfully (reason: {quiz_result['assignment_reason']}). "
            "Your journey with MirrorGPT begins now."
        )

        logger.info(
            f"Quiz calculated for user {user_id}: {quiz_result['final_archetype']} "
            f"(reason: {quiz_result['assignment_reason']}, scores: {quiz_result['total_scores']})"
        )

        return ArchetypeQuizResponse(
            success=True,
            data=quiz_data,
            message=msg,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Quiz submission failed: {str(e)}")
        import traceback

        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Quiz submission failed: {str(e)}")


@router.get("/quiz/results")
async def get_my_quiz_results(
    current_user: Dict[str, Any] = Depends(get_current_user),
    orchestrator: MirrorOrchestrator = Depends(get_mirror_orchestrator),
):
    """
    Get authenticated user's quiz results and archetype profile

    Used for cross-device sync after login
    """
    try:
        user_id = current_user["id"]

        # Access DynamoDBService through orchestrator
        dynamodb = orchestrator.dynamodb_service

        # Get archetype profile
        profile = await dynamodb.get_user_archetype_profile(user_id)

        # Get quiz results
        quiz_results = await dynamodb.get_user_quiz_results(user_id)

        if not profile and not quiz_results:
            return {
                "success": True,
                "data": None,
                "message": "No quiz data found for this user",
            }

        return {
            "success": True,
            "data": {
                "profile": profile,
                "quiz_results": quiz_results,
            },
            "message": "Quiz data retrieved successfully",
        }

    except Exception as e:
        logger.error(f"Error fetching quiz results: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch quiz results: {str(e)}"
        )


@router.get("/quiz/history")
async def get_quiz_history(
    current_user: Dict[str, Any] = Depends(get_current_user),
    orchestrator: MirrorOrchestrator = Depends(get_mirror_orchestrator),
):
    """
    Get user's quiz history and initial archetype setup

    Returns information about when the user completed their initial archetype quiz
    and what their starting archetype was before any conversation-based evolution.
    """

    try:
        # Get user's archetype profile which contains quiz data
        profile = await orchestrator._get_user_profile(current_user["id"])

        if not profile:
            return {
                "success": True,
                "data": {
                    "has_completed_quiz": False,
                    "message": (
                        "No archetype quiz found. " "Please complete the initial quiz."
                    ),
                },
            }

        quiz_data = profile.get("quiz_data", {})

        if not quiz_data:
            return {
                "success": True,
                "data": {
                    "has_completed_quiz": False,
                    "message": "No quiz data found in profile.",
                },
            }

        return {
            "success": True,
            "data": {
                "has_completed_quiz": True,
                "initial_archetype": quiz_data.get("initial_archetype"),
                "quiz_version": quiz_data.get("quiz_version"),
                "completed_at": quiz_data.get("completed_at"),
                "current_archetype": profile.get("current_archetype_stack", {}).get(
                    "primary"
                ),
                "evolution_count": len(profile.get("archetype_evolution", [])),
                "last_updated": profile.get("updated_at"),
            },
        }

    except Exception as e:
        logger.error(f"Failed to get quiz history: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Failed to get quiz history: {str(e)}"
        )


@router.get("/profile", response_model=ArchetypeProfileResponse)
async def get_archetype_profile(
    current_user: Dict[str, Any] = Depends(get_current_user),
    orchestrator: MirrorOrchestrator = Depends(get_mirror_orchestrator),
):
    """
    Get user's complete archetype profile including evolution history

    Returns the user's current archetype profile, recent signal patterns,
    and evolution summary showing how their archetypal patterns have changed over time.
    """

    try:
        profile = await orchestrator._get_user_profile(current_user["id"])
        signals = await orchestrator._get_recent_signals_from_messages(
            current_user["id"], limit=10
        )

        evolution_summary = {
            "total_sessions": len(signals),
            "primary_archetype": (
                profile.get("current_archetype_stack", {}).get("primary")
                if profile
                else None
            ),
            "stability_score": (
                profile.get("current_archetype_stack", {}).get("stability_score")
                if profile
                else None
            ),
            "last_updated": profile.get("updated_at") if profile else None,
        }

        profile_data = ArchetypeProfileData(
            user_id=current_user["id"],
            current_profile=profile,
            recent_signals=signals,
            evolution_summary=evolution_summary,
        )

        return ArchetypeProfileResponse(success=True, data=profile_data)

    except Exception as e:
        logger.error(f"Failed to get profile: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get profile: {str(e)}")


@router.get("/signals", response_model=EchoSignalResponse)
async def get_echo_signals(
    limit: int = Query(
        default=20, le=100, ge=1, description="Number of signals to retrieve"
    ),
    archetype_filter: Optional[str] = Query(
        default=None, description="Filter by primary archetype"
    ),
    current_user: Dict[str, Any] = Depends(get_current_user),
    orchestrator: MirrorOrchestrator = Depends(get_mirror_orchestrator),
):
    """
    Get user's MirrorGPT signals from conversation messages

    Retrieves the user's signal history showing the 5-signal analysis results
    from conversation messages. Can be filtered by archetype to see
    patterns for specific archetypal states.
    """

    try:
        # Use conversation messages instead of echo_signals table
        if archetype_filter:
            # Get signals from conversation messages with archetype filter
            signals = await orchestrator._get_recent_signals_from_messages(
                user_id=current_user["id"],
                limit=limit,
            )
        else:
            signals = await orchestrator._get_recent_signals_from_messages(
                current_user["id"], limit=limit
            )

        # Format signals for response
        formatted_signals = []
        for signal in signals:
            echo_signal = EchoSignalData(
                signal_id=signal.get("message_id", ""),
                timestamp=signal.get("timestamp", ""),
                emotional_resonance=signal.get("signal_1_emotional_resonance", {}),
                symbolic_language=signal.get("signal_2_symbolic_language", {}),
                archetype_blend=signal.get("signal_3_archetype_blend", {}),
                narrative_position=signal.get("signal_4_narrative_position", {}),
                motif_loops=signal.get("signal_5_motif_loops", {}),
                confidence_scores=signal.get("confidence_scores", {}),
            )
            formatted_signals.append(echo_signal)

        return EchoSignalResponse(success=True, data=formatted_signals)

    except Exception as e:
        logger.error(f"Failed to get signals: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get signals: {str(e)}")


@router.get("/moments", response_model=MirrorMomentResponse)
async def get_mirror_moments(
    limit: int = Query(
        default=10, le=50, ge=1, description="Number of moments to retrieve"
    ),
    acknowledged_only: bool = Query(
        default=False, description="Filter for acknowledged moments only"
    ),
    current_user: Dict[str, Any] = Depends(get_current_user),
    orchestrator: MirrorOrchestrator = Depends(get_mirror_orchestrator),
):
    """
    Get user's Mirror Moments - significant transformation points

    Retrieves detected Mirror Moments which represent significant shifts in
    archetypal patterns, breakthroughs, or pattern loop transformations.
    """

    try:
        moments = await orchestrator.dynamodb_service.get_user_mirror_moments(
            user_id=current_user["id"],
            limit=limit,
            acknowledged_only=acknowledged_only,
        )

        # Format moments for response
        formatted_moments = [
            MirrorMomentData(
                moment_id=moment["moment_id"],
                triggered_at=moment["triggered_at"],
                moment_type=moment["moment_type"],
                description=moment["description"],
                significance_score=moment["significance_score"],
                suggested_practice=moment["suggested_practice"],
                acknowledged=moment.get("acknowledged", False),
                acknowledged_at=moment.get("acknowledged_at"),
            )
            for moment in moments
        ]

        return MirrorMomentResponse(success=True, data=formatted_moments)

    except Exception as e:
        logger.error(f"Failed to get moments: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get moments: {str(e)}")


@router.post(
    "/moments/{moment_id}/acknowledge", response_model=MirrorMomentAcknowledgeResponse
)
async def acknowledge_mirror_moment(
    moment_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    orchestrator: MirrorOrchestrator = Depends(get_mirror_orchestrator),
):
    """
    Acknowledge a Mirror Moment

    Marks a Mirror Moment as acknowledged by the user, indicating they have
    recognized and integrated the insight or transformation.
    """

    try:
        success = await orchestrator.dynamodb_service.acknowledge_mirror_moment(
            user_id=current_user["id"], moment_id=moment_id
        )

        if not success:
            raise HTTPException(
                status_code=404,
                detail="Mirror moment not found or already acknowledged",
            )

        return MirrorMomentAcknowledgeResponse(
            success=True, message="Mirror Moment acknowledged successfully"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to acknowledge moment: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Failed to acknowledge moment: {str(e)}"
        )


@router.get("/loops", response_model=PatternLoopResponse)
async def get_pattern_loops(
    active_only: bool = Query(default=True, description="Filter for active loops only"),
    current_user: Dict[str, Any] = Depends(get_current_user),
    orchestrator: MirrorOrchestrator = Depends(get_mirror_orchestrator),
):
    """
    Get user's pattern loops - recurring psychological themes

    Retrieves detected pattern loops which represent recurring psychological
    themes or motifs in the user's expressions and thoughts.
    """

    try:
        loops = await orchestrator.dynamodb_service.get_user_pattern_loops(
            user_id=current_user["id"], active_only=active_only
        )

        # Format loops for response
        formatted_loops = [
            PatternLoopData(
                loop_id=loop["loop_id"],
                elements=loop["elements"],
                strength_score=loop["strength_score"],
                trend=loop["trend"],
                first_seen=loop["first_seen"],
                last_seen=loop["last_seen"],
                occurrence_count=loop["occurrence_count"],
                transformation_detected=loop["transformation_detected"],
                archetype_context=loop["archetype_context"],
            )
            for loop in loops
        ]

        return PatternLoopResponse(success=True, data=formatted_loops)

    except Exception as e:
        logger.error(f"Failed to get loops: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get loops: {str(e)}")


@router.get("/insights", response_model=UserInsightsResponse)
async def get_pattern_insights(
    current_user: Dict[str, Any] = Depends(get_current_user),
    orchestrator: MirrorOrchestrator = Depends(get_mirror_orchestrator),
):
    """
    Get personalized pattern insights and growth indicators

    Generates comprehensive insights about the user's archetypal journey,
    signal patterns, and growth indicators based on their historical data.
    """

    try:
        insights_data = await orchestrator.get_user_insights(current_user["id"])

        if "error" in insights_data:
            raise HTTPException(status_code=500, detail=insights_data["error"])

        insights = UserInsightsData(
            archetype_journey=insights_data["archetype_journey"],
            signal_patterns=insights_data["signal_patterns"],
            growth_indicators=insights_data["growth_indicators"],
        )

        return UserInsightsResponse(success=True, data=insights)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to generate insights: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Failed to generate insights: {str(e)}"
        )


@router.get("/archetypes/list")
async def get_archetype_list(current_user: Dict[str, Any] = Depends(get_current_user)):
    """
    Get list of all available archetypes with descriptions

    Returns the complete list of 14 archetypes supported by MirrorGPT
    along with their core resonances and symbolic languages.
    """

    try:
        from ..utils.archetype_data import ArchetypeDefinitions

        archetypes = ArchetypeDefinitions.get_all_archetypes()

        archetype_list = []
        for name, data in archetypes.items():
            archetype_list.append(
                {
                    "name": name,
                    "core_resonance": data.get("core_resonance", ""),
                    "tone": data.get("tone", ""),
                    "symbolic_language": data.get("symbolic_language", []),
                    "transformation_key": data.get("transformation_key"),
                }
            )

        return {
            "success": True,
            "data": {"archetypes": archetype_list, "total_count": len(archetype_list)},
        }

    except Exception as e:
        logger.error(f"Failed to get archetype list: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Failed to get archetype list: {str(e)}"
        )


@router.get("/session/greeting")
async def get_session_greeting(
    current_user: Dict[str, Any] = Depends(get_user_with_profile),
    orchestrator: MirrorOrchestrator = Depends(get_mirror_orchestrator),
    conversation_service: "ConversationService" = Depends(get_conversation_service),
):
    """
    Get personalized opening message for a new MirrorGPT session.

    Generates a grounded, context-aware opening based on:
    - User's current pattern profile
    - Recent pattern signals
    - Continuity memory from previous conversations (summaries)
    - Mirror Moments and behavioral indicators
    """

    try:
        # These four reads are independent — run them concurrently instead of
        # sequentially (greeting was ~5 serial DDB round-trips on the read path).
        (
            profile,
            recent_signals,
            recent_moments,
            continuity,
        ) = await asyncio.gather(
            orchestrator._get_user_profile(current_user["id"]),
            orchestrator._get_recent_signals_from_messages(current_user["id"], limit=5),
            orchestrator.dynamodb_service.get_user_mirror_moments(
                current_user["id"], limit=3
            ),
            _load_continuity_context(
                user_id=current_user["id"],
                conversation_service=conversation_service,
            ),
        )

        # Extract user context
        user_context = {
            "id": current_user["id"],
            "name": current_user.get("name") or "",
        }

        # Generate personalized greeting
        greeting_message = await generate_personalized_greeting(
            user_context=user_context,
            profile=profile,
            recent_signals=recent_signals,
            recent_moments=recent_moments,
            orchestrator=orchestrator,
            continuity=continuity,
        )

        return {
            "success": True,
            "data": {
                "greeting_message": greeting_message,
                "session_id": str(uuid.uuid4()),
                "timestamp": datetime.utcnow().isoformat(),
                "user_archetype": (
                    profile.get("current_archetype_stack", {}).get("primary")
                    if profile
                    else None
                ),
                "archetype_confidence": (
                    profile.get("current_archetype_stack", {}).get("confidence_score")
                    if profile
                    else None
                ),
                # Continuity: the client should echo conversation_id back on
                # /chat so the same thread continues. has_prior_context tells
                # the UI whether the greeting references prior work.
                "conversation_id": continuity.get("resume_conversation_id"),
                "has_prior_context": continuity.get("has_prior_context", False),
            },
        }

    except Exception as e:
        logger.error(f"Failed to generate greeting: {str(e)}")
        fallback_name = _sanitize_name(current_user.get("name"))
        name_part = f", {fallback_name}" if fallback_name else ""
        greeting = f"Welcome back{name_part}. What's on your mind today?"
        return {
            "success": True,
            "data": {
                "greeting_message": greeting,
                "session_id": str(uuid.uuid4()),
                "timestamp": datetime.utcnow().isoformat(),
                "user_archetype": None,
                "archetype_confidence": None,
                "conversation_id": None,
                "has_prior_context": False,
            },
        }


async def generate_personalized_greeting(
    user_context: Dict[str, Any],
    profile: Optional[Dict[str, Any]],
    recent_signals: List[Dict[str, Any]],
    recent_moments: List[Dict[str, Any]],
    orchestrator: MirrorOrchestrator,
    continuity: Optional[Dict[str, Any]] = None,
) -> str:
    """Generate a session opening using the master MirrorGPT system prompt.

    The opening always addresses the member by first name (when known) and,
    when continuity context is present, MUST acknowledge it stance-first
    ("feeling stuck", "working through") before topic, without quoting
    user text.
    """
    user_name = _sanitize_name(user_context.get("name"))
    name_for_header = f" for {user_name}" if user_name else ""
    # Mandatory addressing rule when we have a name. The master prompt
    # already restricts to first-name only; this just ensures the opening
    # actually uses it instead of falling back to a generic "Welcome".
    name_instruction = (
        f"You MUST address them by their first name — {user_name} — "
        "once in this opening. Use it naturally (not necessarily at the "
        "very start). "
        if user_name
        else ""
    )
    continuity = continuity or {}
    context_lines: List[str] = continuity.get("context_lines") or []
    has_prior = bool(context_lines)
    is_returning = has_prior or bool(profile or recent_signals)

    if has_prior:
        context_block = "\n".join(context_lines)
        trigger = (
            f"Open a new MirrorGPT session{name_for_header}. They are a "
            "returning user.\n\n"
            "Continuity context, ordered most recent first (background "
            "only — do not quote, do not treat as the user's current "
            "message):\n"
            f"{context_block}\n\n"
            "Write a single short opening message (1–2 sentences). "
            f"{name_instruction}"
            "You MUST acknowledge the prior context briefly — anchor on "
            "the FIRST line only (their most recent conversation); the "
            "older lines are light background and must not be recapped — "
            "stance first (how they were sitting with it — e.g. 'feeling "
            "stuck', 'working through', 'sitting with') and then the "
            "topic in a few words — then invite them to continue or shift "
            "focus. Use plain grounded language. Do not quote their "
            "words. Obey the anti-oracle, safety, and banned-language "
            "rules from the system prompt."
        )
    else:
        returning_clause = (
            "They are a returning user." if is_returning else "They are a new user."
        )
        trigger = (
            f"Open a new session{name_for_header}. {returning_clause} "
            f"{name_instruction}"
            "Write a single short opening message that invites them to "
            "share what is on their mind."
        )

    try:
        messages = [
            ChatMessage("system", MIRRORGPT_SYSTEM_PROMPT),
            ChatMessage("user", trigger),
        ]
        generated_greeting = await orchestrator.openai_service.send_async(messages)
        return generated_greeting.strip()

    except Exception as e:
        logger.error(f"Failed to generate GPT greeting: {str(e)}")
        name_clause = f", {user_name}" if user_name else ""
        if is_returning:
            return f"Welcome back{name_clause}. What's been on your mind?"
        else:
            return f"Hey{name_clause}, good to have you here. What's going on for you today?"


@router.get("/health")
async def mirrorgpt_health():
    """
    MirrorGPT service health check

    Provides health status for MirrorGPT-specific services including
    archetype engine, pattern detection, and database connectivity.
    """

    try:
        # Basic health check for MirrorGPT services
        from ..utils.archetype_data import ArchetypeDefinitions

        # Check if archetype data is accessible
        archetypes = ArchetypeDefinitions.get_all_archetypes()
        archetype_count = len(archetypes)

        # Check if symbol library is accessible
        symbols = ArchetypeDefinitions.get_symbol_library()
        symbol_categories = len(symbols)

        return {
            "status": "healthy",
            "service": "MirrorGPT",
            "timestamp": datetime.utcnow().isoformat(),
            "components": {
                "archetype_engine": "operational",
                "pattern_detection": "operational",
                "symbol_processing": "operational",
            },
            "metrics": {
                "available_archetypes": archetype_count,
                "symbol_categories": symbol_categories,
                "version": "1.0.0",
            },
        }

    except Exception as e:
        logger.error(f"MirrorGPT health check failed: {str(e)}")
        return {
            "status": "degraded",
            "service": "MirrorGPT",
            "timestamp": datetime.utcnow().isoformat(),
            "error": str(e),
        }
