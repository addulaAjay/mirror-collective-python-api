# 🚀 Local Development Setup Guide

This guide will help you run the Mirror Collective Python API locally with DynamoDB.

## Prerequisites

- Python 3.12+
- AWS CLI configured (for DynamoDB Local)
- Docker (recommended for DynamoDB Local)
- Virtual environment set up

## 📋 Step-by-Step Setup

### 1. Environment Setup

First, create your local environment file:

```bash
# Copy the example environment file
cp .env.example .env.local

# Edit the file with your local settings
```

Your `.env.local` should contain:

```bash
# AWS Configuration
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=fake-local-key
AWS_SECRET_ACCESS_KEY=fake-local-secret

# DynamoDB Local Configuration
DYNAMODB_ENDPOINT_URL=http://localhost:8000
DYNAMODB_USERS_TABLE=users-local
DYNAMODB_ACTIVITY_TABLE=user_activity-local
DYNAMODB_CONVERSATIONS_TABLE=mirror-conversations-local
DYNAMODB_MESSAGES_TABLE=mirror-conversation-messages-local

# MirrorGPT Tables
MIRRORGPT_SIGNALS_TABLE=mirrorgpt_signals-local
MIRRORGPT_MOMENTS_TABLE=mirrorgpt_moments-local
MIRRORGPT_LOOPS_TABLE=mirrorgpt_loops-local
MIRRORGPT_INSIGHTS_TABLE=mirrorgpt_insights-local
MIRRORGPT_ARCHETYPES_TABLE=mirrorgpt_archetypes-local

# OpenAI Configuration (get from OpenAI dashboard)
OPENAI_API_KEY=your-actual-openai-api-key

# Cognito Configuration (for testing - use real values or mock)
COGNITO_USER_POOL_ID=us-east-1_EXAMPLE123
COGNITO_CLIENT_ID=example123client456
COGNITO_CLIENT_SECRET=example-secret-if-needed

# Environment
ENVIRONMENT=development
DEBUG=true

# Feature Flags
ENABLE_CONVERSATION_PERSISTENCE=true
ENABLE_CONVERSATION_SEARCH=false
ENABLE_MESSAGE_ENCRYPTION=false

# Performance Settings
MAX_CONTEXT_MESSAGES=30
MAX_TOKENS_PER_CONVERSATION=4000
CONVERSATION_CACHE_TTL=3600
MESSAGE_BATCH_SIZE=25
```

### 2. Install Dependencies

```bash
# Activate your virtual environment
source .venv/bin/activate

# Install all dependencies
pip install -r requirements.txt -r requirements-dev.txt
```

### 3. Start DynamoDB Local

#### Option A: Docker (Recommended)

```bash
# Pull and run DynamoDB Local
docker run -p 8000:8000 amazon/dynamodb-local -jar DynamoDBLocal.jar -sharedDb -inMemory
```

#### Option B: Direct Download

```bash
# Download DynamoDB Local (if not using Docker)
wget https://s3.us-west-2.amazonaws.com/dynamodb-local/dynamodb_local_latest.tar.gz
tar -xzf dynamodb_local_latest.tar.gz
java -Djava.library.path=./DynamoDBLocal_lib -jar DynamoDBLocal.jar -sharedDb -port 8000
```

### 4. Create Database Tables

```bash
# Load environment variables
export $(cat .env.local | xargs)

# Create all required tables
python scripts/create_dynamodb_tables.py
python scripts/create_conversation_tables.py
python scripts/create_mirrorgpt_tables_clean.py
```

### 5. Verify Database Setup

```bash
# List tables to verify creation
aws dynamodb list-tables --endpoint-url http://localhost:8000 --region us-east-1
```

You should see tables like:
- users-local
- user_activity-local
- mirror-conversations-local
- mirror-conversation-messages-local
- mirrorgpt_signals-local
- mirrorgpt_moments-local
- mirrorgpt_loops-local
- mirrorgpt_insights-local
- mirrorgpt_archetypes-local

### 6. Start the API Server

```bash
# Load environment variables and start server
export $(cat .env.local | xargs)
uvicorn src.app.handler:app --reload --port 8001 --host 0.0.0.0

# Or use the startup script
python -c "
import os
from dotenv import load_dotenv
load_dotenv('.env.local')
"
uvicorn src.app.handler:app --reload --port 8001
```

## 🧪 Testing Your Setup

### 1. Health Check

```bash
curl http://localhost:8001/health
```

Expected response:
```json
{
  "status": "healthy",
  "service": "Mirror Collective Python API",
  "timestamp": "2025-09-08T..."
}
```

### 2. Test MirrorGPT Chat (requires OpenAI API key)

```bash
curl -X POST http://localhost:8001/api/mirrorgpt/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer fake-jwt-for-local-testing" \
  -d '{
    "message": "Hello, I want to understand my communication patterns",
    "include_archetype_analysis": true,
    "use_enhanced_response": true
  }'
```

### 3. Test Database Connection

```bash
# Run the integration test
python scripts/test_mirrorgpt_integration.py
```

## 🔧 Troubleshooting

### DynamoDB Connection Issues

```bash
# Check if DynamoDB Local is running
curl http://localhost:8000

# Check AWS credentials (they can be fake for local)
aws configure list

# Verify endpoint configuration
echo $DYNAMODB_ENDPOINT_URL
```

### Environment Variable Issues

```bash
# Debug environment loading
python -c "
import os
from dotenv import load_dotenv
load_dotenv('.env.local')
print('DYNAMODB_ENDPOINT_URL:', os.getenv('DYNAMODB_ENDPOINT_URL'))
print('OPENAI_API_KEY:', 'SET' if os.getenv('OPENAI_API_KEY') else 'NOT SET')
"
```

### OpenAI API Issues

If you don't have an OpenAI API key, you can mock the responses:

```bash
# Set mock mode (add to .env.local)
OPENAI_MOCK_MODE=true
```

## 📁 File Structure for Local Development

```
mirror_collective_python_api/
├── .env.local                 # Your local environment variables
├── scripts/
│   ├── create_dynamodb_tables.py
│   ├── create_conversation_tables.py
│   └── create_mirrorgpt_tables_clean.py
├── src/app/
│   ├── handler.py            # FastAPI app entry point
│   ├── services/
│   │   ├── dynamodb_service.py
│   │   └── mirror_orchestrator.py
│   └── api/
│       └── mirrorgpt_routes.py
```

## 🔄 Development Workflow

1. **Start DynamoDB Local** (keep running in terminal 1)
2. **Load environment** and **start API server** (terminal 2)
3. **Make changes** to code (auto-reloads with --reload flag)
4. **Test endpoints** with curl or your frontend
5. **View DynamoDB data** using AWS CLI or DynamoDB admin tools

## 🎯 Quick Start Commands

Here's everything in one script:

```bash
#!/bin/bash
# quick-start.sh

# 1. Start DynamoDB Local in background
docker run -d -p 8000:8000 --name dynamodb-local amazon/dynamodb-local -jar DynamoDBLocal.jar -sharedDb -inMemory

# 2. Wait for DynamoDB to start
sleep 3

# 3. Load environment and create tables
export $(cat .env.local | xargs)
python scripts/create_dynamodb_tables.py
python scripts/create_conversation_tables.py
python scripts/create_mirrorgpt_tables_clean.py

# 4. Start the API server
uvicorn src.app.handler:app --reload --port 8001
```

Run with: `chmod +x quick-start.sh && ./quick-start.sh`

## 🛑 Cleanup

```bash
# Stop and remove DynamoDB Local container
docker stop dynamodb-local
docker rm dynamodb-local

# Or if running locally without Docker
# Kill the Java process running DynamoDB Local
```

You're now ready to develop locally with full DynamoDB integration! 🎉
