"""
MirrorGPT API Routes
Extends existing API structure with MirrorGPT-specific endpoints
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..core.security import get_current_user
from ..services.dynamodb_service import DynamoDBService
from ..services.mirror_orchestrator import MirrorOrchestrator
from ..services.openai_service import OpenAIService
from .models import (
    ArchetypeAnalysisData,
    ArchetypeAnalysisRequest,
    ArchetypeAnalysisResponse,
    ArchetypeProfileData,
    ArchetypeProfileResponse,
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

# Create router for MirrorGPT endpoints
router = APIRouter(prefix="/mirrorgpt", tags=["MirrorGPT"])


def get_mirror_orchestrator():
    """Dependency injection for MirrorOrchestrator"""
    dynamodb_service = DynamoDBService()
    openai_service = OpenAIService()
    return MirrorOrchestrator(dynamodb_service, openai_service)


@router.post("/chat", response_model=MirrorGPTChatResponse)
async def mirror_chat(
    request: MirrorGPTChatRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
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

        # Extract user context for personalized response
        user_context = {
            "id": current_user["id"],
            "name": (
                current_user.get("name")
                or current_user.get("given_name")
                or current_user.get("email", "").split("@")[0]
                if current_user.get("email")
                else None
            ),
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
            raise HTTPException(
                status_code=500, detail=result.get("error", "Unknown error")
            )

        # Format response data
        chat_data = MirrorGPTChatData(
            message_id=str(uuid.uuid4()),
            response=result["response"],
            archetype_analysis=result["archetype_analysis"],
            change_detection=result["change_detection"],
            suggested_practice=result.get("suggested_practice"),
            confidence_breakdown=result["confidence_breakdown"],
            session_metadata=result["session_metadata"],
        )

        return MirrorGPTChatResponse(success=True, data=chat_data)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Mirror chat failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Mirror chat failed: {str(e)}")


@router.post("/analyze", response_model=ArchetypeAnalysisResponse)
async def analyze_archetype(
    request: ArchetypeAnalysisRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    orchestrator: MirrorOrchestrator = Depends(get_mirror_orchestrator),
):
    """
    Standalone archetype analysis without response generation

    Provides detailed archetype detection and pattern analysis for a given message
    without generating a conversational response. Useful for analysis tools and dashboards.
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
    from conversation messages. Can be filtered by archetype to see patterns for specific archetypal states.
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
