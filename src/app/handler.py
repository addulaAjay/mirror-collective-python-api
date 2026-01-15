import logging
import os
import time

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum

# Triggering reload to pick up new ENVs



load_dotenv()

from src.app.services.scheduler import start_scheduler

from .api.mirrorgpt_routes import router as mirrorgpt_router
from .api.models import HealthResponse
from .api.routes import router as api_router
from .core.error_handlers import setup_error_handlers
from .core.logging_config import setup_logging

# Setup logging first
setup_logging()
logger = logging.getLogger("app.handler")


app = FastAPI(
    title="Mirror Collective Python API",
    version="1.0.0",
    description="""
    ## Mirror Collective Python API

    RESTful API for Mirror Collective platform with comprehensive authentication and chat capabilities.

    ### Features
    - 🔐 AWS Cognito Authentication
    - 💬 AI-powered Chat Mirror
    - 🔒 JWT Token Management
    - 📧 Email Services with AWS SES
    - 🛡️ Rate Limiting and Security Headers
    - 🔄 Password Reset Functionality

    ### Authentication
    Most endpoints require authentication via JWT tokens obtained from the `/api/auth/login` endpoint.
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
        f"{request.method} {request.url.path} - {response.status_code} ({duration:.3f}s)"
    )

    return response


# Rate limiting middleware
@app.middleware("http")
async def rate_limiting_middleware(request: Request, call_next):
    from .core.rate_limiting import rate_limit_middleware

    await rate_limit_middleware(request)
    response = await call_next(request)
    return response


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
        "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'",
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
        "description": "RESTful API for Mirror Collective platform with comprehensive authentication",
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

@app.on_event("startup")
def startup_event():
    start_scheduler() 

# Mount main API routes under /api to mirror Node structure
app.include_router(api_router, prefix="/api")

# Mount MirrorGPT routes under /api
app.include_router(mirrorgpt_router, prefix="/api")

handler = Mangum(app)
