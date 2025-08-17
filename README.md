# Mirror Collective Python API (Serverless FastAPI)

This is a parity FastAPI implementation of the core Node.js API pieces (auth placeholder + chat mirror + health) deployable via Serverless Framework on AWS Lambda + HTTP API.

## Features Implemented
- /health endpoint
- /api/auth/login (stub implementation - NOT secure; replace with Cognito SRP flow)
- /api/auth/me (decodes unverified JWT claims for parity; add JWKS verification for production)
- /api/chat/mirror with request validation and simple echo response
- CORS, security headers, structured models
- In-memory rate limit placeholder (can be extended with DynamoDB/Redis)

## Project Structure
```
python_api/
  serverless.yml
  requirements.txt
  src/
    app/
      handler.py          # Lambda entry (Mangum)
      api/
        routes.py         # Routes
        models.py         # Pydantic models
      core/
        security.py       # JWT decode helpers
```

## Local Development
Install dependencies:
```
pip install -r requirements.txt
```
Run locally:
```
uvicorn src.app.handler:app --reload --port 8001
```
Test:
```
curl http://localhost:8001/health
curl -X POST http://localhost:8001/api/chat/mirror -H 'Content-Type: application/json' -d '{"message":"Hello"}'
```

## Deployment
Prerequisites: Serverless Framework with Python requirements plugin.
```
cd python_api
STAGE=staging serverless deploy
```

## Security TODOs
- Implement real Cognito auth (InitiateAuth + RespondToAuthChallenge)
- Verify JWT signatures using Cognito JWKS (instead of unverified decode)
- Replace stub tokens
- Persistent rate limiting store

## Notes
This is a starting point for migration or dual-runtime support alongside the existing Node.js API.
