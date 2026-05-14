from dotenv import load_dotenv

# Triggering reload to pick up new ENVs
load_dotenv()

import logging
import os
import time

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum

from .api.echo_routes import router as echo_router
from .api.echo_v1_routes import router as echo_v1_router
from .api.me_routes import router as me_router
from .api.mirrorgpt_routes import router as mirrorgpt_router
from .api.models import HealthResponse
from .api.practice_routes import router as practice_router
from .api.reflection_routes import router as reflection_router
from .api.routes import router as api_router
from .api.subscription_routes import router as subscription_router
from .api.telemetry_routes import router as telemetry_router
from .core.error_handlers import setup_error_handlers
from .core.logging_config import setup_logging
from .core.quota_middleware import QuotaEnforcementMiddleware

# Setup logging first
setup_logging()
logger = logging.getLogger("app.handler")


app = FastAPI(
    title="Mirror Collective Python API",
    version="1.0.0",
    description="""
    ## Mirror Collective Python API

    RESTful API for Mirror Collective platform with comprehensive
    authentication and chat capabilities.

    ### Features
    - 🔐 AWS Cognito Authentication
    - 💬 AI-powered Chat Mirror
    - 🔒 JWT Token Management
    - 📧 Email Services with AWS SES
    - 🛡️ Rate Limiting and Security Headers
    - 🔄 Password Reset Functionality

    ### Authentication
    Most endpoints require authentication via JWT tokens obtained
    from the `/api/auth/login` endpoint.
    Include the token in the `Authorization` header as `Bearer <token>`.

    ### Rate Limiting
    API requests are rate-limited to 100 requests per minute per IP address.
    """,
    contact={
        "name": "Mirror Collective API Support",
        "email": "support@mirrorcollective.com",
    },
    license_info={
        "name": "MIT",
    },
    debug=os.getenv("DEBUG", "false").lower() == "true",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# Setup CORS
allowed_origins = [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Basic request logging middleware (lightweight)
@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    start_time = time.time()

    response = await call_next(request)

    duration = time.time() - start_time
    logger.info(
        f"{request.method} {request.url.path} - "
        f"{response.status_code} ({duration:.3f}s)"
    )

    return response


# Rate limiting middleware
@app.middleware("http")
async def rate_limiting_middleware(request: Request, call_next):
    from .core.rate_limiting import rate_limit_middleware

    await rate_limit_middleware(request)
    response = await call_next(request)
    return response


# Quota enforcement middleware for Echo Vault — registered as a real ASGI
# middleware so its `__init__` (which constructs DynamoDBService +
# StorageQuotaService) runs ONCE at app startup instead of on every request.
app.add_middleware(QuotaEnforcementMiddleware)


# Security headers middleware
@app.middleware("http")
async def security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    headers = {
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "X-XSS-Protection": "1; mode=block",
        "Referrer-Policy": "no-referrer",
        "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'"
        ),
        "X-API-Version": "1.0.0",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    for k, v in headers.items():
        response.headers.setdefault(k, v)
    return response


# Setup comprehensive error handling
setup_error_handlers(app)


# API information endpoint for service discovery
@app.get("/api")
async def api_info():
    """API information endpoint"""
    return {
        "message": "Mirror Collective API v1.0.0",
        "description": (
            "RESTful API for Mirror Collective platform "
            "with comprehensive authentication"
        ),
        "version": "1.0.0",
        "features": [
            "User Authentication with AWS Cognito",
            "JWT Token Management",
            "Email Services with AWS SES",
            "Rate Limiting and Security",
            "Password Reset Functionality",
        ],
        "endpoints": {
            "auth": "/api/auth",
            "users": "/api/users",
            "collections": "/api/collections",
        },
        "documentation": {"auth": "/api/auth/docs", "health": "/api/auth/health"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# Health check endpoint
@app.get("/health", response_model=HealthResponse)
async def health():
    """Basic health check endpoint"""
    return HealthResponse(
        status="healthy",
        service="Mirror Collective Python API",
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )


# Detailed health check with dependencies
@app.get("/health/detailed")
async def detailed_health():
    """Detailed health check with dependency validation"""
    from .core.health_checks import HealthCheckService

    health_service = HealthCheckService()
    return await health_service.run_all_checks()


# Health check under /api for consistency
@app.get("/api/health", response_model=HealthResponse)
async def api_health():
    """API health check endpoint"""
    return HealthResponse(
        status="healthy",
        service="Mirror Collective Python API",
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )


# Mount main API routes under /api to mirror Node structure
app.include_router(api_router, prefix="/api")

# Mount MirrorGPT routes under /api
app.include_router(mirrorgpt_router, prefix="/api")

# Mount Echo Vault routes under /api
app.include_router(echo_router, prefix="/api")

# Mount Subscription routes
app.include_router(subscription_router)

# Mount Reflection Room V1 routes under /api
app.include_router(reflection_router, prefix="/api")
app.include_router(echo_v1_router, prefix="/api")
app.include_router(practice_router, prefix="/api")
app.include_router(me_router, prefix="/api")
app.include_router(telemetry_router, prefix="/api")

# lifespan="off" skips Starlette's startup/shutdown probe on every cold start.
# We had no real lifespan handlers to run anyway (the previous on_event(startup)
# kicked off an in-process BackgroundScheduler that never fires reliably on
# Lambda — actual cron is handled by the trialExpirationCheck and
# echoReleaseScheduler functions in serverless.yml).
handler = Mangum(app, lifespan="off")
