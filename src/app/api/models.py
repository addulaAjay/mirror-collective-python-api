import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator


class ConversationTurn(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)


class MirrorChatRequest(BaseModel):
    message: str = Field(min_length=1)
    conversationHistory: Optional[List[ConversationTurn]] = None
    userName: Optional[str] = None


class MirrorChatResponse(BaseModel):
    success: bool = True
    data: Dict[str, Any]  # Contains reply and timestamp from use case


# Enhanced models for conversation management
class EnhancedMirrorChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=5000)
    conversationId: Optional[str] = None
    userName: Optional[str] = None
    createNewConversation: bool = False


class EnhancedMirrorChatResponse(BaseModel):
    success: bool = True
    data: Dict[str, Any]  # Contains reply, timestamp, conversation metadata


class ConversationSummaryResponse(BaseModel):
    conversationId: str
    title: str
    lastMessageAt: str
    messageCount: int
    isArchived: bool = False


class ConversationListResponse(BaseModel):
    success: bool = True
    data: Dict[str, Any]  # Contains conversations list and metadata


class ConversationDetailResponse(BaseModel):
    success: bool = True
    data: Dict[str, Any]  # Contains conversation details and recent messages


class ConversationManagementRequest(BaseModel):
    conversationId: str = Field(min_length=1)


class UpdateConversationTitleRequest(BaseModel):
    conversationId: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=100)


class UserRegistrationRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    fullName: str = Field(min_length=2, max_length=100, pattern=r"^[a-zA-Z\s\'-]+$")

    @field_validator("password")
    @classmethod
    def validate_password(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters long")
        if not re.search(r"[a-z]", v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not re.search(r"\d", v):
            raise ValueError("Password must contain at least one digit")
        if not re.search(r"[@$!%*?&]", v):
            raise ValueError("Password must contain at least one special character")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    resetCode: str = Field(min_length=1)
    newPassword: str = Field(min_length=8)

    @field_validator("newPassword")
    @classmethod
    def validate_new_password(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters long")
        if not re.search(r"[a-z]", v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not re.search(r"\d", v):
            raise ValueError("Password must contain at least one digit")
        if not re.search(r"[@$!%*?&]", v):
            raise ValueError("Password must contain at least one special character")
        return v


class RefreshTokenRequest(BaseModel):
    refreshToken: str = Field(min_length=1)


class EmailVerificationRequest(BaseModel):
    email: EmailStr
    verificationCode: str = Field(min_length=1)


class ResendVerificationCodeRequest(BaseModel):
    email: EmailStr


class TokenBundle(BaseModel):
    accessToken: str
    refreshToken: str


class UserBasic(BaseModel):
    id: str
    email: EmailStr
    fullName: str
    isVerified: bool


class AuthResponse(BaseModel):
    success: bool = True
    data: Optional[Dict[str, Any]] = None
    message: Optional[str] = None


class LoginResponse(BaseModel):
    success: bool = True
    data: Dict[str, Any]


class GeneralApiResponse(BaseModel):
    success: bool
    message: Optional[str] = None


class ErrorDetail(BaseModel):
    field: str
    message: str


class ValidationErrorResponse(BaseModel):
    success: bool = False
    error: str
    message: Optional[str] = None
    validationErrors: Optional[List[ErrorDetail]] = None
    requestId: Optional[str] = None
    timestamp: str


class ApiErrorResponse(BaseModel):
    success: bool = False
    error: str
    message: Optional[str] = None
    details: Optional[Any] = None
    requestId: Optional[str] = None
    timestamp: str


class HealthResponse(BaseModel):
    status: str
    service: str
    timestamp: str


# ========================================
# MIRRORGPT API MODELS
# ========================================


class MirrorGPTChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=5000)
    session_id: Optional[str] = None
    conversation_id: Optional[str] = None
    include_archetype_analysis: bool = True
    use_enhanced_response: bool = True


class ArchetypeAnalysisRequest(BaseModel):
    message: str = Field(min_length=1, max_length=5000)
    session_context: Optional[List[str]] = None


class ArchetypeAnalysisData(BaseModel):
    primary_archetype: str
    secondary_archetype: Optional[str] = None
    confidence_score: float
    symbolic_elements: List[str]
    emotional_markers: Dict[str, Any]
    narrative_position: Dict[str, Any]
    active_motifs: List[str]
    archetype_description: str


class ArchetypeAnalysisResponse(BaseModel):
    success: bool = True
    data: ArchetypeAnalysisData


class EchoSignalData(BaseModel):
    signal_id: str
    timestamp: str
    emotional_resonance: Dict[str, Any]
    symbolic_language: Dict[str, Any]
    archetype_blend: Dict[str, Any]
    narrative_position: Dict[str, Any]
    motif_loops: Dict[str, Any]
    confidence_scores: Dict[str, float]


class EchoSignalResponse(BaseModel):
    success: bool = True
    data: List[EchoSignalData]


class MirrorMomentData(BaseModel):
    moment_id: str
    triggered_at: str
    moment_type: str
    description: str
    significance_score: float
    suggested_practice: str
    acknowledged: bool
    acknowledged_at: Optional[str] = None


class MirrorMomentResponse(BaseModel):
    success: bool = True
    data: List[MirrorMomentData]


class MirrorGPTChatData(BaseModel):
    message_id: str
    response: str
    archetype_analysis: Dict[str, Any]
    change_detection: Dict[str, Any]
    suggested_practice: Optional[str] = None
    confidence_breakdown: Dict[str, float]
    session_metadata: Dict[str, Any]


class MirrorGPTChatResponse(BaseModel):
    success: bool = True
    data: MirrorGPTChatData


class PatternLoopData(BaseModel):
    loop_id: str
    elements: List[str]
    strength_score: float
    trend: str
    first_seen: str
    last_seen: str
    occurrence_count: int
    transformation_detected: bool
    archetype_context: str


class PatternLoopResponse(BaseModel):
    success: bool = True
    data: List[PatternLoopData]


class UserInsightsData(BaseModel):
    archetype_journey: Dict[str, Any]
    signal_patterns: Dict[str, Any]
    growth_indicators: Dict[str, Any]


class UserInsightsResponse(BaseModel):
    success: bool = True
    data: UserInsightsData


class ArchetypeProfileData(BaseModel):
    user_id: str
    current_profile: Optional[Dict[str, Any]]
    recent_signals: List[Dict[str, Any]]
    evolution_summary: Dict[str, Any]


class ArchetypeProfileResponse(BaseModel):
    success: bool = True
    data: ArchetypeProfileData


class AcknowledgeMirrorMomentRequest(BaseModel):
    moment_id: str = Field(min_length=1)


class MirrorMomentAcknowledgeResponse(BaseModel):
    success: bool = True
    message: str
